import json
from typing import Any, List, Dict

# アプリケーション種類ごとの設定
with open('app.json', "r", encoding="utf-8") as file:
    confApp = json.load(file)

# 端末1台が持つデータ構造
class app:
    id: int
    useTime: float
    useTimeSchedule: float
    def __init__(self):
        self.id = 0
        self.useTime=0
        self.useTimeSchedule=0

class Term:
    #インターフェース的に定義
    id: int
    apBssid: int
    lines : List
    app: object
    appNum: int
    
    #初期化（コンストラクタ）
    def __init__(self) :
        self.id = 0
        self.apBssid = 0
        self.appNum = 0
        
        self.app=app()

        self.lines = [
            {
                "id": 0,
                "dataLimit": 3 * 1024, # MB
                "transferSend": 0,
                "transferRecieve": 0,
                "transferRecieveTp": 0,
                "price": 7000
            },
            {
                "id": 1,
                "dataLimit": 2 * 1024, # MB
                "transferSend": 0,
                "transferRecieve": 0,
                "transferRecieveTp": 0,
                "price": 5000
            },
            {
                "id": 2,
                "dataLimit": 1 * 1024, # MB
                "transferSend": 0,
                "transferRecieve": 0,
                "transferRecieveTp": 0,
                "price": 3000
            }
        ]
  
    def setBaseData(self, id: int, appNum: int):
        self.id = id
        self.appNum = appNum
    
    #def setApInfo(ap: object[]) {
        #// ap.forEach(item => {this.lines.push(item)})
    #}
    
    def setSwitchAp(self, bssid: int):
        self.apBssid = bssid
    
    def setAppNum(self, appNum: int, useTime: int):
        self.appNum = appNum
        self.app.id = appNum
        self.app.useTime = 0 # アプリ利用時間0
        self.app.useTimeSchedule = useTime # 利用予定時間を設定
    
    def useApp(self, sec: float):
        lineNum = self.apBssid
        appNum = self.appNum

        # 現時点ではダウンロードの転送量だけを加算
        self.lines[int(lineNum)]["transferRecieve"] += confApp[appNum]["incTP"] / 8 * sec  #MB
        #print('lineTransferRecieve=' + str(self.lines[lineNum]["transferRecieve"]))

        # 現時点の転送量TP値を設定（接続時TP計算用）
        self.lines[int(lineNum)]["transferRecieveTp"] = confApp[appNum]["incTP"] # Mbps

        # 現在のアプリ利用時間更新
        self.app.useTime += sec
        # print("transferReceiveTP="+ str(self.lines[int(lineNum)]["transferRecieveTp"]))