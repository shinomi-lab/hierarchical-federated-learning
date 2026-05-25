from os.path import dirname, abspath
import sys, json
from typing import List

parent_dir = dirname(abspath(__file__))
if parent_dir not in sys.path: # 追加
    sys.path.append(parent_dir) # 追加

from term import Term  # 端末1台が持つデータ構造 
from ap import Ap  #1基地局が持つデータ構造

with open('sim.json', "r", encoding="utf-8") as file:
    confSim = json.load(file)

# エリア内の基地局生成
def createAp(apNum: int):
    aps: List[Ap] = []
    for apIndex in range(apNum):
        AP = Ap()
        AP.setBaseData('bssid' + str(apIndex), 0, 0)
        AP.setTermCapa(confSim["termCapa"]) # 基地局収容数
        aps.append(AP)
    return aps

set
# エリア内の端末生成
def createTerm(termNum: int):
    terms: List[Term] = []
    # const ap = new Ap()
    for i in range(termNum):
        TERM = Term()
        TERM.setBaseData(i, 0)
        terms.append(TERM)
    return terms