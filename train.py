"""""""""
Pytorch implementation of "A simple neural network module for relational reasoning"
"""""""""
from __future__ import print_function

import argparse
import json
import os
import pickle
import re
import numpy as np

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.utils import clip_grad_norm
from torch.utils.data import DataLoader
from torchvision import transforms
from tqdm import tqdm, trange

import utils
from clevr_dataset_connector import ClevrDataset, ClevrDatasetStateDescription
from model import RN

import pdb

def train(data, model, optimizer, epoch, args):
    model.train()

    avg_loss = 0.0
    n_batches = 0
    progress_bar = tqdm(data)
    for batch_idx, sample_batched in enumerate(progress_bar):
        img, qst, label = utils.load_tensor_data(sample_batched, args.cuda, args.invert_questions)

        # forward and backward pass
        optimizer.zero_grad()
        output = model(img, qst)
        loss = F.nll_loss(output, label)
        loss.backward()

        # Gradient Clipping
        if args.clip_norm:
            clip_grad_norm(model.parameters(), args.clip_norm)

        optimizer.step()

        # Show progress
        progress_bar.set_postfix(dict(loss=loss.data[0]))
        avg_loss += loss.data[0]
        n_batches += 1

        if batch_idx % args.log_interval == 0:
            avg_loss /= n_batches
            processed = batch_idx * args.batch_size
            n_samples = len(data) * args.batch_size
            progress = float(processed) / n_samples
            print('Train Epoch: {} [{}/{} ({:.0%})] Train loss: {}'.format(
                epoch, processed, n_samples, progress, avg_loss))
            avg_loss = 0.0
            n_batches = 0


def test(data, model, epoch, dictionaries, args):
    model.eval()

    # accuracy for every class
    class_corrects = {}
    # for every class, among all the wrong answers, how much are non pertinent
    class_invalids = {}
    # total number of samples for every class
    class_n_samples = {}
    # initialization
    for c in dictionaries[2].values():
        class_corrects[c] = 0
        class_invalids[c] = 0
        class_n_samples[c] = 0

    corrects = 0.0
    invalids = 0.0
    n_samples = 0

    inverted_answ_dict = {v: k for k,v in dictionaries[1].items()}
    sorted_classes = sorted(dictionaries[2].items(), key=lambda x: hash(x[1]) if x[1]!='number' else int(inverted_answ_dict[x[0]]))
    sorted_classes = [c[0]-1 for c in sorted_classes]

    confusion_matrix_target = []
    confusion_matrix_pred = []

    sorted_labels = sorted(dictionaries[1].items(), key=lambda x: x[1])
    sorted_labels = [c[0] for c in sorted_labels]
    sorted_labels = [sorted_labels[c] for c in sorted_classes]

    avg_loss = 0.0
    progress_bar = tqdm(data)
    for batch_idx, sample_batched in enumerate(progress_bar):
        img, qst, label = utils.load_tensor_data(sample_batched, args.cuda, args.invert_questions, volatile=True)
        
        output = model(img, qst)
        pred = output.data.max(1)[1]

        loss = F.nll_loss(output, label)

        # compute per-class accuracy
        pred_class = [dictionaries[2][o+1] for o in pred]
        real_class = [dictionaries[2][o+1] for o in label.data]
        for idx,rc in enumerate(real_class):
            class_corrects[rc] += (pred[idx] == label.data[idx])
            class_n_samples[rc] += 1

        for pc, rc in zip(pred_class,real_class):
            class_invalids[rc] += (pc != rc)

        for p,l in zip(pred, label.data):
            confusion_matrix_target.append(sorted_classes.index(l))
            confusion_matrix_pred.append(sorted_classes.index(p))
        
        # compute global accuracy
        corrects += (pred == label.data).sum()
        assert corrects == sum(class_corrects.values()), 'Number of correct answers assertion error!'
        invalids = sum(class_invalids.values())
        n_samples += len(label)
        assert n_samples == sum(class_n_samples.values()), 'Number of total answers assertion error!'
        
        avg_loss += loss.data[0]

        if batch_idx % args.log_interval == 0:
            accuracy = corrects / n_samples
            invalids_perc = invalids / n_samples
            progress_bar.set_postfix(dict(acc='{:.2%}'.format(accuracy), inv='{:.2%}'.format(invalids_perc)))
    
    avg_loss /= len(data)
    invalids_perc = invalids / n_samples      
    accuracy = corrects / n_samples

    print('Test Epoch {}: Accuracy = {:.2%} ({:g}/{}); Invalids = {:.2%} ({:g}/{}); Test loss = {}'.format(epoch, accuracy, corrects, n_samples, invalids_perc, invalids, n_samples, avg_loss))
    for v in class_n_samples.keys():
        accuracy = 0
        invalid = 0
        if class_n_samples[v] != 0:
            accuracy = class_corrects[v] / class_n_samples[v]
            invalid = class_invalids[v] / class_n_samples[v]
        print('{} -- acc: {:.2%} ({}/{}); invalid: {:.2%} ({}/{})'.format(v,accuracy,class_corrects[v],class_n_samples[v],invalid,class_invalids[v],class_n_samples[v]))

    # dump results on file
    filename = os.path.join(args.test_results_dir, 'test.pickle')
    dump_object = {
        'class_corrects':class_corrects,
        'class_invalids':class_invalids,
        'class_total_samples':class_n_samples,
        'confusion_matrix_target':confusion_matrix_target,
        'confusion_matrix_pred':confusion_matrix_pred,
        'confusion_matrix_labels':sorted_labels,
        'global_accuracy':accuracy
    }
    pickle.dump(dump_object, open(filename,'wb'))


def main(args):
    args.model_dirs = './model_{}_b{}_lr{}'.format(args.model, args.batch_size, args.lr)
    args.features_dirs = './features'
    if not os.path.exists(args.model_dirs):
        os.makedirs(args.model_dirs)

    args.test_results_dir = './test_results'
    if not os.path.exists(args.test_results_dir):
        os.makedirs(args.test_results_dir)

    args.cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    print('Building word dictionaries from all the words in the dataset...')
    dictionaries = utils.build_dictionaries(args.clevr_dir)
    print('Word dictionary completed!')

    print('Initializing CLEVR dataset...')
    
    if(not args.state_description):
        train_transforms = transforms.Compose([transforms.Resize((128, 128)),
                                           transforms.Pad(8),
                                           transforms.RandomCrop((128, 128)),
                                           transforms.RandomRotation(2.8),  # .05 rad
                                           transforms.ToTensor()])
        test_transforms = transforms.Compose([transforms.Resize((128, 128)),
                                          transforms.ToTensor()])
                                          
        clevr_dataset_train = ClevrDataset(args.clevr_dir, True, dictionaries, train_transforms)
        clevr_dataset_test = ClevrDataset(args.clevr_dir, False, dictionaries, test_transforms)

        # Use a weighted sampler for training:
        weights = clevr_dataset_train.answer_weights()
        sampler = torch.utils.data.sampler.WeightedRandomSampler(weights, len(weights))
        
        # Initialize Clevr dataset loaders
        clevr_train_loader = DataLoader(clevr_dataset_train, batch_size=args.batch_size,
                                        sampler=sampler, num_workers=8, collate_fn=utils.collate_samples_image)
        clevr_test_loader = DataLoader(clevr_dataset_test, batch_size=args.batch_size,
                                       shuffle=False, num_workers=8, collate_fn=utils.collate_samples_image)
    else:
        clevr_dataset_train = ClevrDatasetStateDescription(args.clevr_dir, True, dictionaries)
        clevr_dataset_test = ClevrDatasetStateDescription(args.clevr_dir, False, dictionaries)
        
        # Initialize Clevr dataset loaders
        clevr_train_loader = DataLoader(clevr_dataset_train, batch_size=args.batch_size,
                                        shuffle=True, num_workers=8, collate_fn=utils.collate_samples_state_description)
        clevr_test_loader = DataLoader(clevr_dataset_test, batch_size=args.batch_size,
                                       shuffle=False, num_workers=8, collate_fn=utils.collate_samples_state_description)

    print('CLEVR dataset initialized!')

    # Build the model
    args.qdict_size = len(dictionaries[0])
    args.adict_size = len(dictionaries[1])
    model = RN(args)

    if torch.cuda.device_count() > 1 and args.cuda:
        model = torch.nn.DataParallel(model)
        model.module.cuda()  # call cuda() overridden method

    if args.cuda:
        model.cuda()

    start_epoch = 1
    if args.resume:
        filename = args.resume
        if os.path.isfile(filename):
            print('==> loading checkpoint {}'.format(filename))
            checkpoint = torch.load(filename)

            #removes 'module' from dict entries, pytorch bug #3805
            #checkpoint = {k.replace('module.',''): v for k,v in checkpoint.items()}

            model.load_state_dict(checkpoint)
            print('==> loaded checkpoint {}'.format(filename))
            start_epoch = int(re.match(r'.*epoch_(\d+).pth', args.resume).groups()[0]) + 1

    
    if args.conv_transfer_learn:
        if os.path.isfile(args.conv_transfer_learn):
            # TODO: there may be problems caused by pytorch issue #3805 if using DataParallel

            print('==> loading conv layer from {}'.format(args.conv_transfer_learn))
            # pretrained dict is the dictionary containing the already trained conv layer
            pretrained_dict = torch.load(args.conv_transfer_learn)

            if torch.cuda.device_count() == 1:
                conv_dict = model.conv.state_dict()
            else:
                conv_dict = model.module.conv.state_dict()
            
            # filter only the conv layer from the loaded dictionary
            conv_pretrained_dict = {k.replace('conv.','',1): v for k, v in pretrained_dict.items() if 'conv.' in k}

            # overwrite entries in the existing state dict
            conv_dict.update(conv_pretrained_dict)

            # load the new state dict
            if torch.cuda.device_count() == 1:
                model.conv.load_state_dict(conv_dict)
                params = model.conv.parameters()
            else:
                model.module.conv.load_state_dict(conv_dict)
                params = model.module.conv.parameters()

            # freeze the weights for the convolutional layer by disabling gradient evaluation
            # for param in params:
            #     param.requires_grad = False

            print("==> conv layer loaded!")
        else:
            print('Cannot load file {}'.format(args.conv_transfer_learn))

    progress_bar = trange(start_epoch, args.epochs + 1)
    if args.test:
        # perform a single test
        print('Testing epoch {}'.format(start_epoch))
        test(clevr_test_loader, model, start_epoch, dictionaries, args)
    else:
        # perform a full training
        optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, weight_decay=1e-4)
        print('Training ({} epochs) is starting...'.format(args.epochs))
        for epoch in progress_bar:
            # TRAIN
            progress_bar.set_description('TRAIN')
            train(clevr_train_loader, model, optimizer, epoch, args)
            # TEST
            progress_bar.set_description('TEST')
            test(clevr_test_loader, model, epoch, dictionaries, args)
            # SAVE MODEL
            filename = 'RN_epoch_{:02d}.pth'.format(epoch)
            torch.save(model.state_dict(), os.path.join(args.model_dirs, filename))


if __name__ == '__main__':
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch Relational-Network CLEVR')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--epochs', type=int, default=350, metavar='N',
                        help='number of epochs to train (default: 350)')
    parser.add_argument('--lr', type=float, default=0.00025, metavar='LR',
                        help='learning rate (default: 0.00025)')
    parser.add_argument('--clip-norm', type=int, default=10,
                        help='max norm for gradients; set to 0 to disable gradient clipping (default: 10)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--resume', type=str,
                        help='resume from model stored')
    parser.add_argument('--clevr-dir', type=str, default='.',
                        help='base directory of CLEVR dataset')
    parser.add_argument('--model', type=str, choices=['original', 'ir'], default='original',
                        help='which model is used to train the network')
    parser.add_argument('--invert-questions', action='store_true', default=False,
                        help='invert the question word indexes for LSTM processing')
    parser.add_argument('--test', action='store_true', default=False,
                        help='perform only a single test. To use with --resume')
    parser.add_argument('--conv-transfer-learn', type=str,
                    help='use convolutional layer from another training')
    parser.add_argument('--state-description', action='store_true', default=False,
                        help='disables CUDA training')

    args = parser.parse_args()
    main(args)
