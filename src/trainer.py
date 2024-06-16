import importlib
import os
from typing import Optional

import hydra
import numpy as np
from omegaconf import OmegaConf, DictConfig
from torch.utils import data
from src.datasets import TrainDataset, ValidationDataset
from src.metrics import compute_metrics
from src.utils import get_device, set_seeds
from tqdm.auto import tqdm
import torch
from torch.nn.utils import clip_grad_norm_
from src.utils import count_parameters
import pprint
import glob
import logging
import datetime


class Trainer:
    def __init__(self, config):
        self.config = config
        self.learning_rate = config.optimizer.learning_rate

        # create the model
        self.model = getattr(importlib.import_module("src.models"), config.model)(
            input_channels=config.image_channels, n_features=config.n_features)

        # define the loss
        self.criterion = getattr(importlib.import_module("torch.nn"), config.loss)()

        # define the optimizer
        self.optimizer = getattr(importlib.import_module("adamp"), config.optimizer.name)(
            params=self.model.parameters(),
            betas=tuple(config.optimizer.betas),
            eps=config.optimizer.eps,
            lr=self.learning_rate
        )

        # get the device
        self.device = get_device()
        self.model = self.model.to(self.device)

        # configure logger
        configuration = OmegaConf.to_object(config)
        pp = pprint.PrettyPrinter()
        pp.pprint(configuration)
        # logger
        now = datetime.time()
        dt_string = now.strftime("%H-%M-%S")
        logging.basicConfig(filename=f"log_{dt_string}.log",
                            filemode='w',
                            level = logging.INFO,
                            format = "%(asctime)s\t%(levelname)s:\t%(message)s.",
                            datefmt = "%Y-%m-%d\t%H:%M:%S")
        self.logger = logging.getLogger()

        # create train dataloader from the given training dataset
        train_dataset = TrainDataset(config.train_dataset.path,
                                     scales=list(config.train_dataset.scales),
                                     degradation=config.train_dataset.degradation,
                                     patch_size=config.train_dataset.patch_size,
                                     augment=config.train_dataset.augment)
        self.train_dataloader = data.DataLoader(train_dataset,
                                                batch_size=config.train_dataset.batch_size,
                                                shuffle=config.train_dataset.shuffle,
                                                collate_fn=TrainDataset.collate_fn,
                                                num_workers=config.train_dataset.num_workers,
                                                pin_memory=config.train_dataset.pin_memory)

        # create validation dataloader from the given validation dataset
        val_dataset = ValidationDataset(config.val_dataset.path,
                                        scale=config.val_dataset.scale,
                                        degradation=config.val_dataset.degradation,
                                        n_images=config.val_dataset.n_images_to_use)
        self.val_dataloader = data.DataLoader(val_dataset,
                                              batch_size=config.val_dataset.batch_size,
                                              shuffle=config.val_dataset.shuffle,
                                              num_workers=config.val_dataset.num_workers,
                                              pin_memory=config.val_dataset.pin_memory)

    def train(self):

        # set initial values for total training epochs and steps
        print("Starting training...")
        finished = False

        # load the checkpoint if required
        if self.config.load_checkpoint:
            checkpoint = self.checkpoint_load()

            # if the checkpoint dictionary is an empty dict, checkpoint is not loaded so initialize values
            if bool(checkpoint):
                # initialize the current epoch metrics by loading from the checkpoint
                epochs = checkpoint["epochs"]
                if self.config.restart_steps_count:
                    self.learning_rate = self.config.optimizer.learning_rate
                    steps = 0
                    for param_group in self.optimizer.param_groups:
                        param_group["lr"] = self.learning_rate
                else:
                    self.learning_rate = checkpoint["learning_rate"]
                    steps = checkpoint["steps"]
                best_train_psnr = checkpoint["best_train_psnr"]
                best_train_ssim = checkpoint["best_train_ssim"]
                best_val_psnr = checkpoint["best_val_psnr"]
                best_val_ssim = checkpoint["best_val_ssim"]
            else:
                steps = 0
                epochs = 0
                best_train_psnr = 0
                best_train_ssim = 0
                best_val_psnr = 0
                best_val_ssim = 0
        else:
            steps = 0
            epochs = 0
            best_train_psnr = 0
            best_train_ssim = 0
            best_val_psnr = 0
            best_val_ssim = 0

        # while the training is not finished (i.e. we haven't reached the max number of training steps)
        while not finished:

            # set the model in training mode since at the end of each epoch the model is set to eval mode by the eval
            # method
            self.model.train()

            # initialize the current epoch metrics
            train_loss = 0
            train_samples = 0
            train_psnr = 0
            train_ssim = 0
            train_sr_hr_comparisons = []

            # for each batch in the training set
            for scale, lrs, hrs in tqdm(self.train_dataloader, position=0):

                # send lr and hr batches to device
                lrs = lrs.to(self.device)
                hrs = hrs.to(self.device)
                batch_size = lrs.size()[0]

                # zero the gradients
                self.optimizer.zero_grad()

                # do forward step in the model to compute sr images
                srs = self.model(lrs, scale)

                # compute loss between srs images and hrs
                loss = self.criterion(srs, hrs)

                # add current loss to the training loss
                train_loss += loss.item() * batch_size
                train_samples += batch_size

                # convert the two image batches to numpy array and reshape to have channels in last dimension
                hrs = hrs.cpu().detach().numpy().transpose(0, 2, 3, 1)
                srs = srs.cpu().detach().numpy().transpose(0, 2, 3, 1)

                # compute the current training metrics
                psnr, ssim = compute_metrics(hrs, srs)

                # add metrics of the current batch to the total sum
                train_psnr += np.sum(psnr)
                train_ssim += np.sum(ssim)

                # create an image containing the sr and hr image side by side and append to the array of comparison
                # images
                sr_hr = np.concatenate((srs[0], hrs[0]), axis=1)
                train_sr_hr_comparisons.append(sr_hr)

                # do a gradient descent step
                loss.backward()
                if self.config.clip is not None:
                    clip_grad_norm_(self.model.parameters(), self.config.clip)
                self.optimizer.step()

                # increment the number of total steps
                steps += 1

                # half learning rate
                if (steps % self.config.optimizer.halving_steps) == 0:
                    halved_lr = self.learning_rate / 2
                    self.learning_rate = max(halved_lr, self.config.optimizer.min_learning_rate)
                    for param_group in self.optimizer.param_groups:
                        param_group["lr"] = self.learning_rate

                # checkpoint
                if (steps % self.config.checkpoint_every) == 0:
                    checkpoint_info = {"learning_rate": self.learning_rate,
                                       "epochs": epochs,
                                       "steps": steps,
                                       "best_train_psnr": best_train_psnr,
                                       "best_train_ssim": best_train_ssim,
                                       "best_val_psnr": best_val_psnr,
                                       "best_val_ssim": best_val_ssim
                                       }

                    # checkpoint the training
                    self.checkpoint_save(checkpoint=checkpoint_info)

                # if number of maximum training steps is reached
                if steps >= self.config.max_training_steps:
                    # finish the training by breaking the for loop and the outer loop
                    finished = True
                    break

            # compute the current epoch training loss
            train_loss /= train_samples

            # compute the average metrics for the current training epoch
            train_psnr = round(train_psnr / train_samples, 2)
            train_ssim = round(train_ssim / train_samples, 4)

            # evaluate the model for each scale at the end of the epoch (when we looped the entire training set) and get
            # the validation loss and metrics
            val_loss, val_psnr, val_ssim, val_sr_hr_comparisons = self.validate()

            # compute the new best train metrics
            best_train_psnr = max(best_train_psnr, train_psnr)
            best_train_ssim = max(best_train_ssim, train_ssim)

            # compute the new best validation metric
            best_val_psnr = max(best_val_psnr, val_psnr)
            best_val_ssim = max(best_val_ssim, val_ssim)

            # print the metrics at the end of the epoch
            print("Epoch:", epochs + 1, "- total_steps:", steps + 1,
                  "\n\tTRAIN",
                  "\n\t- train loss:", train_loss,
                  "\n\t- train psnr:", train_psnr,
                  "\n\t- best train psnr:", best_train_psnr,
                  "\n\t- train ssim:", train_ssim,
                  "\n\t- best train ssim:", best_train_ssim,
                  "\n\tVAL",
                  "\n\t- val loss:", val_loss,
                  "\n\t- val psnr:", val_psnr,
                  "\n\t- best val psnr:", best_val_psnr,
                  "\n\t- val ssim:", val_ssim,
                  "\n\t- best val ssim:", best_val_ssim)

            # log metrics to the logger at each training step if required
            if self.logger:
                self.logger.info(f"Epoch: {epochs + 1} - total_steps: {steps + 1}\n\tTRAIN \n\t- train loss: {train_loss}\n\t- train psnr: {train_psnr}\n\t- best train psnr: {best_train_psnr}\n\t- train ssim: {train_ssim}\n\t- best train ssim: {best_train_ssim}\n\tVAL\n\t- val loss: {val_loss}\n\t- val psnr: {val_psnr}\n\t- best val psnr: {best_val_psnr}\n\t- val ssim: {val_ssim}\n\t- best val ssim: {best_val_ssim}")

            # increment number of epochs
            epochs += 1

        print("Training finished! Saving model...")
        self.save(self.config.output_model_file)
        print("Done!")

    def validate(self):
        print("Evaluating...")

        # set model to eval mode
        self.model.eval()

        # initialize current validation epoch metrics
        val_samples = 0
        val_loss = 0
        val_psnr = 0
        val_ssim = 0
        val_sr_hr_comparisons = []

        # disable gradient computation
        with torch.no_grad():
            for scale, lr, hr in tqdm(self.val_dataloader, position=0):
                # send lr and hr to device
                lr = lr.to(self.device)
                hr = hr.to(self.device)
                batch_size = lr.size()[0]

                # do forward step in the model to compute sr images
                sr = self.model(lr, scale)

                # compute the validation loss for the current scale
                loss = self.criterion(sr, hr)
                val_loss += loss.item() * batch_size
                val_samples += batch_size

                # convert the two image batches to numpy array and reshape to have channels in last dimension
                hr = hr.cpu().detach().numpy().transpose(0, 2, 3, 1)
                sr = sr.cpu().detach().numpy().transpose(0, 2, 3, 1)

                # comupute psnr and ssim for the current validation sample
                psnr, ssim = compute_metrics(hr, sr)

                # add metrics of the current batch to the total sum
                val_psnr += np.sum(psnr)
                val_ssim += np.sum(ssim)

                # create an image containing the sr and hr image side by side and append to the array of comparison
                # images
                sr_hr = np.concatenate((sr[0], hr[0]), axis=1)
                val_sr_hr_comparisons.append(sr_hr)

            # compute the average val loss for the current validation epoch
            val_loss /= val_samples

            # compute the average metrics for the current validation epoch
            val_psnr = round(val_psnr / val_samples, 2)
            val_ssim = round(val_ssim / val_samples, 4)

        return val_loss, val_psnr, val_ssim, val_sr_hr_comparisons

    def save(self, filename: str):
        filename = f"{filename}.pt"
        trained_model_path = self.config.model_folder
        if not os.path.isdir(trained_model_path):
            os.makedirs(trained_model_path)
        file_path = f"{trained_model_path}{filename}"

        print(f"Saving trained model to {file_path}...")

        # save network weights
        torch.save(self.model.state_dict(), file_path)

    def load(self, filename: str) -> None:
        filename = f"{filename}.pt"
        trained_model_path = self.config.model_folder
        if os.path.isdir(trained_model_path):
            file_path = f"{trained_model_path}{filename}"
            if os.path.isfile(file_path):
                print(f"Loading model from {file_path}...")
                weights = torch.load(file_path, map_location=torch.device("cuda"))
                self.model.load_state_dict(weights)
                print("Done!")
            else:
                print("Weights file not found.")
        else:
            print("The directory of the trained models does not exist.")

    def checkpoint_save(self, checkpoint: dict) -> None:
        print(f"Checkpointing at step {checkpoint['steps']}...")
        checkpoint_path = f"{self.config.model_folder}checkpoints/"
        if not os.path.isdir(checkpoint_path):
            os.makedirs(checkpoint_path)
        file_path = f"{checkpoint_path}{self.config.checkpoint_file}.pt"

        checkpoint['model_weights'] = self.model.state_dict()
        checkpoint['optimizer_weights'] = self.optimizer.state_dict()

        # remove old checkpoints to save storage
        folder = glob.glob(f"{checkpoint_path}*")
        for file in folder:
            os.remove(file)

        # checkpoint the training
        torch.save(checkpoint, file_path)

    def checkpoint_load(self) -> dict:
        checkpoint_path = f"{self.config.model_folder}checkpoints/"
        # checkpoint_path = f"{self.config.model_folder}"
        
        # if the folder with checkpoints exists and contains the checkpoint file
        if os.path.isdir(checkpoint_path):
            checkpoint_file_path = f"{checkpoint_path}{self.config.checkpoint_file}.pt"
            if os.path.isfile(checkpoint_file_path):
                # load checkpoint information from the file
                print(f"Loading checkpoint from file {checkpoint_file_path}...")
                checkpoint = torch.load(checkpoint_file_path, map_location=torch.device("cpu"))

                self.model.load_state_dict(checkpoint['model_weights'])
                self.optimizer.load_state_dict(checkpoint['optimizer_weights'])

                return checkpoint
            else:
                # no file exists in the folder, so return None
                print("Checkpoint file does not exist. Training is starting from the beginning...")
                return {}
        else:
            # the checkpoint folder does not exist, so return None
            print("Checkpoint folder does not exist. Training is starting from the beginning...")
            return {}


@hydra.main(version_base=None, config_path="../config/", config_name="training")
def main(config: DictConfig):
    # set seeds for reproducibility
    if config.seed:
        set_seeds(config.seed)

    # create trainer with the given testing configuration
    trainer = Trainer(config)
    count_parameters(trainer.model)

    # run the training
    trainer.train()


if __name__ == "__main__":
    main()
