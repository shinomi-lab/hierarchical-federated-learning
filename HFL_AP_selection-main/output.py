from fileinput import filename
from os.path import dirname, abspath
import sys
import json
import math as Math
from typing import List

#import * as fs from "fs"; # ファイル出力
with open('sim.json', "r", encoding="utf-8") as file:
    confSim = json.load(file)

def satisLimit(satisMovingAve: List, termNumOverLimitArray: List):
    # 出力ファイル名
    algoName: str = ""
    ALGO_NUM = confSim["algo"]["algoNum"]

    # 出力先ディレクトリ
    OUT_DIR = confSim["output"]["outFileDir"]

    match ALGO_NUM:
        case 0:
          algoName = "random"
        case 4:
          algoName = "utilA"
        case 5:
          algoName = "utilB"
        case 6:
          algoName = "utilC"
        case 7:
          algoName = "hungarian"
        case 8:
          algoName = "hungarian-capa"

    simTime: str = ""
    if (confSim["type"] == "A") :
        simTime = "time-" + str(confSim["simNumTime"])
    elif (confSim["type"] == "C"):
        simTime = "interval-" + str(confSim["iterate"]["intervalSec"])
      
    FILENAME: str = str(OUT_DIR + str(confSim["type"]) +"_" + "-" + simTime + "_" + algoName + "_" + str(confSim["termNum"]) + ".txt")

    # データ整形
    #print(satisMovingAve)
    textSatis = "\n".join(str(_) for _ in satisMovingAve)
    textTerm = "\n".join(str(_) for _ in termNumOverLimitArray)
    text = textSatis + "\n\n" + textTerm;

    # 非同期書き込み
    try:
        with open(FILENAME, "w") as f:
            f.write(text)
            print("write end")
    except:
        print("Write Error!!")
      
    return text
