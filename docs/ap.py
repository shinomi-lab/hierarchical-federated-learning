#1基地局が持つデータ構造

class Ap:
    #型注釈
    bssid: str
    rtt: float
    tp: float
    termNum: int
    termCapa: int

    #インスタンス（コンストラクタ）
    def __init__(self):
        self.bssid = ''
        self.rtt = 0
        self.tp = 0
        self.termNum = 0
        self.termCapa = 0
        
    def setBaseData(self, bssid: str, rtt: float, tp: float) :
        self.bssid = bssid
        self.rtt = rtt
        self.tp = tp
  
    def setRtt(self, rtt: float):
        self.rtt = rtt

    def setTp(self, tp: float):
        self.tp = tp

    def setTermNum(self, termNum: int):
        self.termNum = termNum
        
    def getTermNum(self):
        return self.termNum

    def isTermCapa(self):
        return True if self.termCapa >= self.termNum else False
  
    def setTermCapa(self, termCapa: int):
        self.termCapa = termCapa