import importlib
import os
import torch
from omegaconf import OmegaConf, DictConfig
from torch.utils import data
import numpy as np
from src.datasets import TestDataset
from src.utils import get_device, set_seeds, count_parameters
from src.exceptions import InvalidTestModeException
from tqdm.auto import tqdm
from src.metrics import compute_metrics
import hydra
import pprint
import matplotlib.pyplot as plt
from PIL import Image
import logging
import datetime
import random

class Tester:
    def __init__(self, config):
        self.config = config

        # get the device
        self.device = get_device()

        # define testing mode
        self.testing_mode = self.config.testing.mode

        # if the testing is a model testing
        if self.testing_mode == "model":

            # create the model
            self.model = getattr(importlib.import_module("src.models"), config.testing.model)(
                input_channels=config.image_channels)

            # move model on device
            self.model = self.model.to(self.device)

            # load the weights from the saved file
            self.load(self.config.testing.output_model_file)

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

        # create test dataloader from the given testing dataset
        test_dataset = TestDataset(config.test_dataset.path,
                                   scale=config.test_dataset.scale,
                                   degradation=config.test_dataset.degradation)
        self.test_dataloader = data.DataLoader(test_dataset,
                                               batch_size=config.test_dataset.batch_size,
                                               shuffle=config.test_dataset.shuffle,
                                               num_workers=config.test_dataset.num_workers,
                                               pin_memory=config.test_dataset.pin_memory)

    def __bicubic_upscale(self, lr, scale):
        # for each image in the batch
        upscaled = []
        for image in lr:
            # compute the output size of the upscaled image
            height = image.shape[0]
            width = image.shape[1]

            # compute the actual upscaled size of the upscaled image
            upscaled_height = int(height * scale)
            upscaled_width = int(width * scale)

            # image is currently in range 0-1 due to DataLoader, so to upscale it using PIL we need to 
            # convert it to range 0-255
            image_255 = image * 255
            image_255 = image_255.astype(np.uint8)
            
            # upscale the image width bicubic
            upscaled_image = Image.fromarray(image_255)
            upscaled_image = np.asarray(upscaled_image.resize((upscaled_width, upscaled_height), Image.Resampling.BICUBIC))

            # restore 0-1 interval
            upscaled_image = upscaled_image / 255
            upscaled_image = upscaled_image.astype(np.float32)

            # append to the batch of upscaled images
            upscaled.append(upscaled_image)
        
        # convert the list of upscaled to numpy array and return
        return np.asarray(upscaled)

    def test(self):
        print("Testing...")

        # set model to eval mode if testing is a model testing
        if self.testing_mode == "model":
            self.model.eval()

        # initialize testing metrics
        test_psnr = 0
        test_ssim = 0
        test_samples = 0
        test_sr_hr_comparisons = []

        # disable gradient computation
        with torch.no_grad():
            for file_name, scale, lr, hr in tqdm(self.test_dataloader, position=0):
                lr = lr.to(self.device)
                hr = hr.to(self.device)
                batch_size = lr.size()[0]

                # if testing is a model testing
                if self.testing_mode == "model":
                    # do forward step in the model to compute sr images
                    sr = self.model(lr, scale)

                    # convert the sr image batch to numpy array and reshape to have channels in last dimension
                    sr = sr.cpu().detach().numpy().transpose(0, 2, 3, 1)

                # otherwise if testing mode is bicubic
                elif self.testing_mode == "bicubic":
                    # convert the lr image batch to numpy array and reshape to have channels in the last dimension
                    lr = lr.cpu().detach().numpy().transpose(0, 2, 3, 1)

                    # compute bicubic upscaled batch of sr images
                    sr = self.__bicubic_upscale(lr, scale)

                # otherwise raise invalid testing mode exception
                else:
                    raise InvalidTestModeException(f"{self.testing_mode} is not a valid testing mode, change it to",
                    "\"bicubic\" or \"model\"")

                for i in range(sr.shape[0]):
                    img = sr[i]
                    
                    # Clip the values to be in the range [0, 255] and convert to uint8
                    img = np.clip(img * 255, 0, 255).astype(np.uint8)
                    
                    # Create an Image object from the numpy array
                    img_pil = Image.fromarray(img)
                    
                    # Save the image
                    img_pil.save(f'tests/{file_name[0].replace(".jpg", ".png")}')
                # convert the hr image batch to numpy rray and reshape to have channels in last dimension
                hr = hr.cpu().detach().numpy().transpose(0, 2, 3, 1)

                # comupute psnr and ssim for the current testing batch
                psnr, ssim = compute_metrics(hr, sr)
                
                # add metrics of the current batch to the total sum
                test_samples += batch_size
                test_psnr += np.sum(psnr)
                test_ssim += np.sum(ssim)

                # create an image containing the sr and hr image side by side and append to the array of comparison
                # images
                sr_hr = np.concatenate((sr[0], hr[0]), axis=1)
                test_sr_hr_comparisons.append(sr_hr)

            # compute the average metrics value for the dataset
            test_psnr = round(test_psnr / test_samples, 2)
            test_ssim = round(test_ssim / test_samples, 4)

            # log the average psnr and ssim of the dataset and the images
            if self.logger:
                self.logger.info(f"test_psnr: {test_psnr} - test_ssim: {test_ssim}")

            # print the metrics at the end of the epoch
            print("Samples:", test_samples,
                  "\n\t- test psnr:", test_psnr,
                  "\n\t- test ssim:", test_ssim)

        return test_psnr, test_ssim, test_sr_hr_comparisons

    def load(self, filename: str) -> None:
        filename = f"{filename}.pt"
        trained_model_path = self.config.testing.model_folder
        print(trained_model_path)
        if os.path.isdir(trained_model_path):
            file_path = f"{trained_model_path}{filename}"
            file_path = 'trained_models/mprnet_final_model.pt'
            if os.path.isfile(file_path):
                print(f"Loading model from {file_path}...")
                weights = torch.load(file_path, map_location=torch.device("cpu"))
                self.model.load_state_dict(weights)
                print("Done!")
            else:
                print("The specified file does not exist in the trained models directory.")
        else:
            print("The directory of the trained models does not exist.")


@hydra.main(version_base=None, config_path="../config/", config_name="testing")
def main(config: DictConfig):
    # set seeds for reproducibility
    if config.seed:
        set_seeds(config.seed)

    # create tester with the given testing configuration
    tester = Tester(config)

    # if testing is a model testing
    if config.testing.mode == "model":
        count_parameters(tester.model)

    # run the test
    tester.test()


if __name__ == "__main__":
    main()
    