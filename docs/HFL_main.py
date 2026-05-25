# Hierarchical Federated Learning

from os.path import dirname, abspath
import sys, os, json, random, copy, cal, rand, create, time
import csv
import numpy as np
import output as output
from typing import List
import os
import copy
import time
import numpy as np
import pickle
from tqdm import tqdm
import torch
from torch.utils.data import TensorDataset, DataLoader
from torch.nn.functional import one_hot
from torch import nn
from torchmetrics.classification import MulticlassAccuracy, MulticlassPrecision, MulticlassRecall, MulticlassF1Score, MulticlassMatthewsCorrCoef
from tensorboardX import SummaryWriter
from sklearn.model_selection import train_test_split
from create_hierarchy import Structure
from options import args_parser
from update import LocalUpdate, test_inference
from models import MLP, CNNMnist, CNNFashion_Mnist, CNNCifar
from utils import get_dataset, exp_details
from sampling import mnist_iid, mnist_noniid, mnist_noniid_unequal

parent_dir = dirname(abspath(__file__))
if parent_dir not in sys.path: # 追加
    sys.path.append(parent_dir)

import matplotlib.pyplot as plt

with open('sim.json', "r", encoding="utf-8") as file:
    confSim = json.load(file)
with open('ap.json', "r", encoding="utf-8") as file:
    confAp = json.load(file)
with open('app.json', "r", encoding="utf-8") as file:
    confAPP = json.load(file)

from term import Term  # 端末1台が持つデータ構造 
from ap import Ap  #1基地局が持つデータ構造
import graph as gp
import hungarian_kai as hung
import output as output
from result import simResult

if __name__ == '__main__':

    start_time = time.time()

    # define paths
    path_project = os.path.abspath('..')
    logger = SummaryWriter('../logs')

    args = args_parser()
    exp_details(args)

    if args.gpu:
        torch.cuda.set_device(args.gpu)
    device = 'cuda' if args.gpu else 'cpu'

    # Read csv file
    n_ap = confSim['apNumMax']
    csv_tpNeed = []
    csv_rttNeed = []
    csv_appNum = []
    csv_ap_states = [[] for _ in range(n_ap * 3)]  # tp, rtt, termNum per AP
    csv_combi = []

    # CSVファイル選択
    import glob
    _pattern = f"{confSim['termNum']}_event*.csv"
    _csv_files = sorted(glob.glob(_pattern))
    if len(_csv_files) == 0:
        file_name = input(f"CSVファイルが見つかりません ({_pattern})。パスを入力: ").strip()
    elif len(_csv_files) == 1:
        file_name = _csv_files[0]
    else:
        print("利用可能なCSVファイル:")
        for _idx, _f in enumerate(_csv_files):
            print(f"  {_idx}: {_f}")
        _sel = input("使用するファイル番号: ").strip()
        file_name = _csv_files[int(_sel)]
    print(f"読み込みファイル: {file_name}")

    with open(file_name, 'r') as f:
        data = csv.reader(f)
        for i, row in enumerate(data):
            if i == 8750:
                break
            else:
                csv_tpNeed.append(int(row[0]))
                csv_rttNeed.append(int(row[1]))
                csv_appNum.append(int(row[2]))
                for j in range(n_ap * 3):
                    csv_ap_states[j].append(float(row[3 + j]))
                csv_combi.append(int(row[3 + n_ap * 3]))

    # load dataset and user groups
    if args.dataset == 'terminal':
        np_tpNeed = np.array(csv_tpNeed)
        np_rttNeed = np.array(csv_rttNeed)
        np_appNum = np.array(csv_appNum)
        np_ap_matrix = np.column_stack([np.array(col) for col in csv_ap_states])
        np_combi = np.array(csv_combi)

        (train_np_tpNeed, test_np_tpNeed,
         train_np_rttNeed, test_np_rttNeed,
         train_np_appNum, test_np_appNum,
         train_ap_matrix, test_ap_matrix,
         train_np_combi, test_np_combi) = train_test_split(
            np_tpNeed, np_rttNeed, np_appNum, np_ap_matrix, np_combi,
            test_size=1./5, random_state=1)

        # train_data
        train_np_tpNeed_norm = (train_np_tpNeed - np.mean(train_np_tpNeed)) / np.std(train_np_tpNeed)
        train_t_tpNeed_norm = torch.from_numpy(train_np_tpNeed_norm).float()
        train_np_rttNeed_norm = (train_np_rttNeed - np.mean(train_np_rttNeed)) / np.std(train_np_rttNeed)
        train_t_rttNeed_norm = torch.from_numpy(train_np_rttNeed_norm).float()
        train_stack = torch.stack([train_t_tpNeed_norm, train_t_rttNeed_norm], axis = 1)
        total_APP = confSim['appNumMax']
        train_t_appNum = torch.tensor(train_np_appNum)
        train_APP_encoded = one_hot(train_t_appNum % total_APP)
        # AP状態特徴量（固定スケーリング: tp/100, rtt/100, termNum/termNum_max）
        _N = float(confSim['termNum'])
        _scale = np.tile([100.0, 100.0, _N], n_ap)
        train_ap_scaled = train_ap_matrix / _scale
        train_ap_tensor = torch.from_numpy(train_ap_scaled).float()
        train_x_data = torch.cat([train_stack, train_APP_encoded, train_ap_tensor], 1).float()
        train_y_data = torch.tensor(train_np_combi)

        # test_data
        test_np_tpNeed_norm = (test_np_tpNeed - np.mean(test_np_tpNeed)) / np.std(test_np_tpNeed)
        test_t_tpNeed_norm = torch.from_numpy(test_np_tpNeed_norm).float()
        test_np_rttNeed_norm = ( test_np_rttNeed - np.mean(test_np_rttNeed)) / np.std( test_np_rttNeed)
        test_t_rttNeed_norm = torch.from_numpy(test_np_rttNeed_norm).float()
        test_stack = torch.stack([test_t_tpNeed_norm, test_t_rttNeed_norm], axis = 1)
        total_APP = confSim['appNumMax']
        test_t_appNum = torch.tensor(test_np_appNum)
        test_APP_encoded = one_hot(test_t_appNum % total_APP)
        test_ap_scaled = test_ap_matrix / _scale
        test_ap_tensor = torch.from_numpy(test_ap_scaled).float()
        test_x_data = torch.cat([test_stack, test_APP_encoded, test_ap_tensor], 1).float()
        test_y_data = torch.tensor(test_np_combi)

        train_dataset = TensorDataset(train_x_data, train_y_data)
        test_dataset = TensorDataset(test_x_data, test_y_data)

        '''
        for example in joint_dataset:
            print(f'x: {example[0]} y: {example[1]}')
        '''

        '''
        torch.manual_seed(1)
        batch_size = confSim['termNum']
        train_dataset = DataLoader(train_joint_dataset, batch_size, shuffle = False)
        test_dataset = DataLoader(test_joint_dataset, batch_size, shuffle = False)
        '''
        
        # sample training data amongst users
        if args.iid:
            # Sample IID user data from Mnist
            user_groups = mnist_iid(train_dataset, args.num_users)
        else:
            # Sample Non-IID user data from Mnist
            if args.unequal:
                # Chose uneuqal splits for every user
                user_groups = mnist_noniid_unequal(train_dataset, args.num_users)
            else:
                # Chose euqal splits for every user
                user_groups = mnist_noniid(train_dataset, args.num_users)
        
    else:
        train_dataset, test_dataset, user_groups = get_dataset(args)

    # BUILD MODEL
    if args.model == 'cnn':
        # Convolutional neural netork
        if args.dataset == 'mnist':
            global_model = CNNMnist(args=args)

        elif args.dataset == 'terminal':
            class Model(nn.Module):
                def __init__(self, input_size, hidden_size, output_size):
                    super().__init__()
                    self.layer1 = nn.Linear(input_size, hidden_size)
                    self.layer2 = nn.Linear(hidden_size, hidden_size)
                    self.layer3 = nn.Linear(hidden_size, output_size)
                    self.relu = nn.ReLU()

                def forward(self, x):
                    x = self.layer1(x)
                    x = self.relu(x)
                    x = self.layer2(x)
                    x = self.relu(x)
                    x = self.layer3(x)
                    x = nn.Softmax(dim=1)(x)
                    return x

                '''
                def __init__(self, input_size, hidden_size, output_size):
                    super().__init__()
                    l1 = nn.Linear(input_size, hidden_size)
                    a1 = nn.ReLU()
                    l2 = nn.Linear(hidden_size, hidden_size)
                    a2 = nn.ReLU()
                    l3 = nn.Linear(hidden_size, output_size)
                    a3 = nn.Softmax(dim=1)
                    l = [l1, a1, l2, a2, l3, a3]
                    self.module_list = nn.ModuleList(l)

                def forward(self, x):
                    for f in self.module_list:
                        x = f(x)
                    return x
                '''

            input_size = train_x_data.shape[1]
            print(input_size)
            hidden_size = confSim['termNum']
            output_size = confSim['apNumMax']
            global_model = Model(input_size, hidden_size, output_size)

        elif args.dataset == 'fmnist':
            global_model = CNNFashion_Mnist(args=args)

        elif args.dataset == 'cifar':
            global_model = CNNCifar(args=args)

    elif args.model == 'mlp':
        # Multi-layer preceptron
        img_size = train_dataset[0][0].shape
        len_in = 1
        for x in img_size:
            len_in *= x
            global_model = MLP(dim_in=len_in, dim_hidden=64,
                               dim_out=args.num_classes)
    else:
        exit('Error: unrecognized model')

    # Set the model to train and send it to device.
    global_model.to(device)
    global_model.train()
    print(global_model)

    # copy weights
    global_weights = global_model.state_dict()

    # create hierarchical structure
    structure = Structure(args, global_weights, global_model, test_dataset)

    # Training
    train_loss, train_accuracy = [], []
    val_acc_list, net_list = [], []
    cv_loss, cv_acc = [], []
    print_every = 2
    val_loss_pre, counter = 0, 0

    for epoch in tqdm(range(args.epochs)):
        local_weights, local_losses = {}, []
        print(f'\n | Global Training Round : {epoch+1} |\n')

        m = max(int(args.frac * args.num_users), 1)
        idxs_users = np.random.choice(range(args.num_users), m, replace=False)

        for idx in idxs_users:
            local_model = LocalUpdate(args=args, dataset=train_dataset, idxs=user_groups[idx], logger=logger)

            user_model, server_idx = structure.get_model(idx, args.download)
            w, loss = local_model.update_weights(model=user_model, global_round=epoch)

            # prepare local weights for uploading weights
            if server_idx in local_weights.keys():
                local_weights[server_idx].append((copy.deepcopy(w)))
            else:
                local_weights[server_idx] = [copy.deepcopy(w)]

            local_losses.append(copy.deepcopy(loss))

        # update system weights
        structure.upload_weights(local_weights)

        # top-down model management
        if args.management:
            print('management is activated')
            structure.model_management()

        # compute the loss
        loss_avg = sum(local_losses) / len(local_losses)
        train_loss.append(loss_avg)

        # Calculate avg training accuracy over all users at every epoch
        list_acc, list_loss = [], []
        global_model.eval()
        for idx in range(args.num_users):
            local_model = LocalUpdate(args=args, dataset=train_dataset,
                                      idxs=user_groups[idx], logger=logger)
            user_model, _ = structure.get_model(idx, args.download)
            acc, loss = local_model.inference(model=user_model)
            list_acc.append(acc)
            list_loss.append(loss)
        train_accuracy.append(sum(list_acc)/len(list_acc))
        
        # # print global training loss after every 'i' rounds
        if (epoch+1) % print_every == 0:
            print(f' \nAvg Training Stats after {epoch+1} global rounds:')
            print(f'Training Loss : {np.mean(np.array(train_loss))}')
            print('Train Accuracy: {:.2f}% \n'.format(100*train_accuracy[-1]))

        '''
        if (epoch+1) % 10 == 0:
            pred_test = global_model(test_x_data)
            print(f'pred_test: {pred_test}')

            correct = (torch.argmax(pred_test, dim=1) == test_y_data).float()
            accuracy = correct.mean()
            print(f'Test Acc.: {accuracy:.4f}')

            precision_metric = MulticlassPrecision(num_classes = confSim['apNumMax'])
            precision = precision_metric(pred_test, test_y_data)
            print(f'Precision: {precision:.4f}')

            recall_metric = MulticlassRecall(num_classes = confSim['apNumMax'])
            recall = recall_metric(pred_test, test_y_data)
            print(f'Recall: {recall:.4f}')
            
            f1_sc_metric = MulticlassF1Score(num_classes = confSim['apNumMax'])
            f1_sc = f1_sc_metric(pred_test, test_y_data)
            print(f'F1 Score: {f1_sc:.4f}')
        '''

    # Test inference after completion of training
    # test_acc, test_loss = test_inference(args, global_model, test_dataset)

    global_model.eval()
    loss, total, correct = 0.0, 0.0, 0.0

    device = 'cuda' if args.gpu else 'cpu'
    loss_fn = nn.CrossEntropyLoss().to(device)
    testloader = DataLoader(test_dataset, batch_size=args.local_bs,
                            shuffle=False)

    pred_test = []
    for batch_idx, (images, labels) in enumerate(testloader):
        images, labels = images.to(device), labels.to(device)

        # Inference
        outputs = global_model(images)
        batch_loss = loss_fn(outputs, labels)
        loss += batch_loss.item()

        # Prediction
        _, pred_labels = torch.max(outputs, 1)
        pred_labels = pred_labels.view(-1)
        correct += torch.sum(torch.eq(pred_labels, labels)).item()
        total += len(labels)
        for index in range(len(pred_labels)):
            pred_test.append(int(pred_labels[index]))

    accuracy = correct/total
    
    print(f' \n Results after {args.epochs} global rounds of training:')
    print("|---- Avg Train Accuracy: {:.2f}%".format(100*train_accuracy[-1]))
    print("|---- Test Accuracy: {:.2f}%".format(100*accuracy))

    pred_test = torch.tensor(pred_test)

    precision_metric = MulticlassPrecision(num_classes = confSim['apNumMax'])
    precision = precision_metric(pred_test, test_y_data)
    print(f'|---- Precision: {100*precision:.2f}%')

    recall_metric = MulticlassRecall(num_classes = confSim['apNumMax'])
    recall = recall_metric(pred_test, test_y_data)
    print(f'|---- Recall: {100*recall:.2f}%')
    
    f1_sc_metric = MulticlassF1Score(num_classes = confSim['apNumMax'])
    f1_sc = f1_sc_metric(pred_test, test_y_data)
    print(f'|---- F1 Score: {100*f1_sc:.2f}%')

    '''
    # Saving the objects train_loss and train_accuracy:
    file_name = './save/{}_{}_{}_C[{}]_iid[{}]_E[{}]_B[{}].pkl'.\
        format(args.dataset, args.model, args.epochs, args.frac, args.iid,args.local_ep, args.local_bs)
    
    with open(file_name, 'wb') as f:
        pickle.dump([train_loss, train_accuracy], f)
    '''

    print('\n Total Run Time: {0:0.4f}'.format(time.time()-start_time))

    # PLOTTING (optional)
    import matplotlib
    import matplotlib.pyplot as plt

    # Plot Loss curve
    plt.figure()
    plt.title('Training Loss vs Communication rounds')
    plt.plot(range(len(train_loss)), train_loss, color='r')
    plt.ylabel('Training loss')
    plt.xlabel('Communication Rounds')
    plt.savefig('./save/hierFed_{}_{}_{}_loss.png'.
                format(args.dataset, args.epochs, confSim['termNum']))
    
    # Plot Average Accuracy vs Communication rounds
    plt.figure()
    plt.title('Average Accuracy vs Communication rounds')
    plt.plot(range(len(train_accuracy)), train_accuracy, color='k')
    plt.ylabel('Average Accuracy')
    plt.xlabel('Communication Rounds')
    plt.savefig('./save/hierFed_{}_{}_{}_acc.png'.
                format(args.dataset, args.epochs, confSim['termNum']))
    
    # Generate AP
    APS: List[Ap] = create.createAp(confSim["apNumMax"]) # 引数: 回線数->term.tsのConstructorと合わせる

    # Generate terminal
    TERMS: List[Term] = create.createTerm(confSim["termNum"])

    # Define variable
    PresatisHarmeanArray: List = []
    satisHarmeanArray: List = []
    termNumOverLimitArray: List = []
    linkTerm: List = [] # The number of terminals connected to each AP
    jtime: List = []
    combi = []
    tpNeed = []
    rttNeed = []
    appNum = []

    # Repeated Assignment
    for i in range(confSim['simNumTime']):
        print("-------------------------------------------------------------")
        print("Number of assignments="+"No."+str(i+1))
        start_time = time.time()
        satisHarmean: float

        # Random setting base station (id) for each tarminal (refer to ap.json)
        rand.randAp(TERMS, APS)

        # Rondom setting Applicatoin (id, useTime, useTimeSchedule) for each terminal (refer to app.json)
        rand.randApp(TERMS, APS)

        # Sum of taminals for each base station
        cal.sumTermAp(TERMS, APS)

        # Calculation of connected TP and RTT for each base station
        cal.calLink(TERMS, APS, confSim["appUseSec"])

        # Harmonized average calculation of pre-assignment terminal satisfaction
        satisHarmean = cal.calSatis(TERMS, APS) # Harmonized average calculation of terminal satisfaction
        PresatisHarmeanArray.append(satisHarmean)
        print("↓")

        appNum = []
        tpNeed = []
        rttNeed = []
        combi = []

        for term in TERMS:
            appNum.append(term.appNum)
            app = cal.calAppNeed(term.appNum)
            tpNeed.append(app.needTP)
            rttNeed.append(app.needRTT)

        np_tpNeed = np.array(tpNeed)
        np_rttNeed = np.array(rttNeed)
        np_appNum = np.array(appNum)

        np_tpNeed_norm = (np_tpNeed - np.mean(np_tpNeed)) / np.std(np_tpNeed)
        t_tpNeed_norm = torch.from_numpy(np_tpNeed_norm).float()
        np_rttNeed_norm = (np_rttNeed - np.mean(np_rttNeed)) / np.std(np_rttNeed)
        t_rttNeed_norm = torch.from_numpy(np_rttNeed_norm).float()
        stack = torch.stack([t_tpNeed_norm, t_rttNeed_norm], axis = 1)
        total_APP = confSim['appNumMax']
        t_appNum = torch.tensor(np_appNum)
        APP_encoded = one_hot(t_appNum % total_APP)
        # AP状態特徴量（訓練時と同じ固定スケーリング）
        _N_infer = float(confSim['termNum'])
        ap_state_row = []
        for ap in APS:
            ap_state_row.extend([ap.tp / 100.0, ap.rtt / 100.0, ap.termNum / _N_infer])
        ap_tensor = torch.tensor([ap_state_row] * len(TERMS), dtype=torch.float)
        x_data = torch.cat([stack, APP_encoded, ap_tensor], 1).float()
        y_data = np.zeros(int(confSim['termNum']))
        y_data = torch.tensor(y_data, dtype=torch.long)

        test_dataset = TensorDataset(x_data, y_data)

        # Predict
        global_model.eval()
        loss, total, correct = 0.0, 0.0, 0.0

        device = 'cuda' if args.gpu else 'cpu'
        loss_fn = nn.CrossEntropyLoss().to(device)
        testloader = DataLoader(test_dataset, batch_size=args.local_bs,
                                shuffle=False)
        
        pred = []
        for batch_idx, (images, labels) in enumerate(testloader):
            images, labels = images.to(device), labels.to(device)

            # Inference
            outputs = global_model(images)
            batch_loss = loss_fn(outputs, labels)
            loss += batch_loss.item()

            # Prediction
            _, pred_labels = torch.max(outputs, 1)
            pred_labels = pred_labels.view(-1)
            correct += torch.sum(torch.eq(pred_labels, labels)).item()
            total += len(labels)
            for index in range(len(pred_labels)):
                pred.append(pred_labels[index])

        accuracy = correct/total

        '''
        print(f' \n Results after {args.epochs} global rounds of training:')
        print("|---- Avg Train Accuracy: {:.2f}%".format(100*train_accuracy[-1]))
        print("|---- Test Accuracy: {:.2f}%".format(100*accuracy))
        '''

        list_apNum = []
        for item in pred:
            list_apNum.append(int(item))

        for item in range(len(TERMS)):
            TERMS[item].setSwitchAp(list_apNum[item])

        print(f'割り当て後の基地局: {list_apNum}')

        # 基地局接続台数
        cal.sumTermAp(TERMS, APS)

        # 接続時RTT & 接続時TP計算
        cal.calLink(TERMS, APS, confSim["appUseSec"])

        # 端末満足度算出
        satisHarmean = cal.calSatis(TERMS, APS) # 端末満足度の調和平均算出
        satisHarmeanArray.append(satisHarmean)

        end_time = time.time()  # 終了時刻
        execution_time = end_time - start_time  # 実行時間の計算
        print(f"実行時間: {execution_time}秒")
        jtime.append(execution_time)

        '''
        #ハンガリアン法
        hung.call_hungarian(TERMS, APS)

        # 基地局接続台数
        cal.sumTermAp(TERMS, APS)

        # 接続時RTT & 接続時TP計算
        cal.calLink(TERMS, APS, confSim["appUseSec"])

        # 端末満足度算出
        satisHarmean = cal.calSatis(TERMS, APS) # 端末満足度の調和平均算出
        satisHarmeanArray.append(satisHarmean)

        # 容量超過端末数算出
        # TERM_NUM_OVER_LIMIT = cal.overTransferLimit(TERMS, APS)
        # termNumOverLimitArray.append(TERM_NUM_OVER_LIMIT)

        # linkTerm.append(copy.deepcopy(cal.sumTermAp(TERMS, APS)))

        end_time = time.time()  # 終了時刻
        execution_time = end_time - start_time  # 実行時間の計算
        print(f"実行時間: {execution_time}秒")
        jtime.append(execution_time)
        '''
        
        
    RES = {
    "satisHarmeanArray": satisHarmeanArray,
    "PresatisHarmeanArray": PresatisHarmeanArray,
    #   "termNumOverLimitArray": termNumOverLimitArray,
    "linkTerm": linkTerm
    }

# グラフ出力

    # 移動平均
    
    SATIS_MOVING_AVE: List = cal.movingAverage(RES["satisHarmeanArray"])

    gp.exportGraph(
            RES["satisHarmeanArray"],
            RES["PresatisHarmeanArray"],
            SATIS_MOVING_AVE,
            TERMS
    )
    
    
    # RES_TEXT: str = output.satisLimit(
    #     SATIS_MOVING_AVE
    # )

    # gp.exportGraph(
    #         RES["satisHarmeanArray"],
    #         SATIS_MOVING_AVE,
    #         RES["termNumOverLimitArray"],
    #         TERMS
    # )
    
    # RES_TEXT: str = output.satisLimit(
    #     SATIS_MOVING_AVE,
    #     RES["termNumOverLimitArray"]
    # )