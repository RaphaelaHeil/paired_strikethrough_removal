import logging
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader
from torchvision.utils import save_image

from src import configuration, metrics
from src.configuration import ModelName, getConfiguration
from src.dataset import PairedDataset
from src.utils import initLoggers, composeTransformations, PadToSize, getModel


class TrainRunner:

    def __init__(self, config: configuration.Configuration):
        self.baseLogger = logging.getLogger('str_ae')
        self.validationLogger = logging.getLogger('val')
        self.lossLogger = logging.getLogger('reconstructionLoss')

        self.config = config
        self.model = getModel(self.config)

        self.baseLogger.info(self.config.modelName)

        self.model = self.model.to(config.device)
        if self.config.loss == "mse":
            self.criterion = torch.nn.MSELoss()
        elif self.config.loss == "bcell" or self.config.modelName == ModelName.TIRAMISU:
            self.criterion = torch.nn.BCEWithLogitsLoss()
        else:
            self.criterion = torch.nn.BCELoss()
        self.optimiser = torch.optim.Adam(self.model.parameters(), lr=self.config.learningRate, betas=self.config.betas)
        self.transformations = composeTransformations(config)
        trainDataset = PairedDataset(config.trainImageDir, fold=self.config.fold, mode="train",
                                     transforms=self.transformations, strokeTypes=self.config.trainStrokeTypes)
        self.trainDataloader = DataLoader(trainDataset, batch_size=self.config.batchSize, shuffle=True, num_workers=1)
        testDataset = PairedDataset(config.trainImageDir, fold=self.config.fold, mode="validation",
                                    transforms=self.transformations, strokeTypes=self.config.testStrokeTypes)
        self.testDataloader = DataLoader(testDataset, batch_size=self.config.batchSize, shuffle=False, num_workers=1)

        self.bestRmse = float('inf')
        self.bestRmseEpoch = 0
        self.bestFscore = float('-inf')
        self.bestFscoreEpoch = 0

    def run(self):
        self.lossLogger.info('epoch,loss')
        self.validationLogger.info('epoch,f1,rmse')

        for epoch in range(1, self.config.epochs + 1):
            epochStartTime = time.time()
            trainLoss = self.trainOneEpoch(epoch)
            run_time = time.time() - epochStartTime
            self.baseLogger.info('[{}/{}], loss: {}, time:{}'.format(epoch, self.config.epochs, trainLoss, run_time))
            if epoch > 1 and self.config.modelSaveEpoch > 0 and epoch % self.config.modelSaveEpoch == 0:
                torch.save(self.model.state_dict(), self.config.outDir / Path('epoch_{}.pth'.format(epoch)))
                self.baseLogger.info('Epoch {}: model saved'.format(epoch))
            if self.config.validationEpoch > 0 and epoch % self.config.validationEpoch == 0:
                self.validateOneEpoch(epoch)
        self.baseLogger.info('Best epochs: RMSE:{} ({}), F1:{} ({})'.format(self.bestRmseEpoch, self.bestRmse,
                                                                            self.bestFscoreEpoch, self.bestFscore))

    def trainOneEpoch(self, epoch):
        self.model.train()
        reconstructionLosses = []
        for batchId, data in enumerate(self.trainDataloader):
            self.optimiser.zero_grad()
            image = data['struck'].to(self.config.device)
            groundTruth = data['groundTruth'].to(self.config.device)
            reconstructed = self.model(image)

            reconstructionLoss = self.criterion(reconstructed, groundTruth)
            reconstructionLoss.backward()
            self.optimiser.step()
            reconstructionLosses.append(reconstructionLoss.item())

        meanReconstructionLoss = np.mean(reconstructionLosses)
        self.lossLogger.info("{},{}".format(epoch, meanReconstructionLoss))
        return meanReconstructionLoss

    def validateOneEpoch(self, epoch):
        self.model.eval()
        rmses = []
        fmeasures = []

        epochdir = self.config.outDir / str(epoch)
        epochdir.mkdir(exist_ok=True, parents=True)

        with torch.no_grad():
            for batchId, data in enumerate(self.testDataloader):
                struckToCleanPairs = torch.Tensor().to(self.config.device)
                struck = data['struck'].to(self.config.device)
                groundTruth = data['groundTruth'].to(self.config.device)
                imageSizes = data["image_size"]

                reconstructed = self.model(struck)
                if self.config.modelName == ModelName.TIRAMISU:
                    reconstructed = torch.sigmoid(reconstructed)

                tmp_struckToClean = torch.cat((groundTruth, struck, reconstructed), 0)
                struckToCleanPairs = torch.cat((struckToCleanPairs, tmp_struckToClean), 0).to(self.config.device)

                save_image(struckToCleanPairs, epochdir / "cleanConcat_e{}_b{}.png".format(epoch, batchId),
                           nrow=self.config.batchSize)

                for idx in range(imageSizes[0].size()[0]):

                    cleanedImage = PadToSize.invert(reconstructed[idx].squeeze().cpu().numpy(),
                                                    (imageSizes[0][idx], imageSizes[1][idx]))
                    groundTruthImage = PadToSize.invert(groundTruth[idx].squeeze().cpu().numpy(),
                                                        (imageSizes[0][idx], imageSizes[1][idx]))
                    rmses.append(metrics.calculateRmse(groundTruthImage, cleanedImage))

                    if self.config.invertImages:
                        fmeasures.append(metrics.calculateF1Score(255.0 - groundTruthImage * 255.0,
                                                                  255.0 - cleanedImage * 255.0, binarise=True)[0])
                    else:
                        fmeasures.append(metrics.calculateF1Score(groundTruthImage * 255.0, cleanedImage * 255.0,
                                                                  binarise=True)[0])

        meanRMSE = np.mean(rmses)
        meanF = np.mean(fmeasures)
        self.baseLogger.info('val [%d/%d], rmse: %f, fmeasure: %f', epoch, self.config.epochs, meanRMSE, meanF)

        self.validationLogger.info("%d,%f,%f", epoch, meanRMSE, meanF)

        if meanRMSE < self.bestRmse:
            self.bestRmseEpoch = epoch
            torch.save({'epoch': epoch, 'model_state_dict': self.model.state_dict(), },
                       self.config.outDir / Path('best_rmse.pth'))
            self.baseLogger.info('%d: Updated best rmse model', epoch)
            self.bestRmse = meanRMSE

        if meanF > self.bestFscore:
            self.bestFscoreEpoch = epoch
            torch.save({'epoch': epoch, 'model_state_dict': self.model.state_dict(), },
                       self.config.outDir / Path('best_fmeasure.pth'))
            self.baseLogger.info('%d: Updated best fmeasure model', epoch)
            self.bestFscore = meanF


if __name__ == "__main__":
    torch.backends.cudnn.benchmark = True

    conf = getConfiguration()

    initLoggers(conf, 'str_ae', ['reconstructionLoss', 'val'])
    logging.getLogger("str_ae").info(conf.fileSection)
    runner = TrainRunner(conf)
    runner.run()
