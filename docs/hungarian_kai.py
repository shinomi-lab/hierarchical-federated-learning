from os.path import dirname, abspath
import sys
import json
import math as Math
import numpy as np
from typing import List
from scipy.optimize import linear_sum_assignment

from term import Term  # 端末1台が持つデータ構造
from ap import Ap  #1基地局が持つデータ構造

parent_dir = dirname(dirname(abspath(__file__)))
if parent_dir not in sys.path: # 追加
    sys.path.append(parent_dir) # 追加

# アプリケーション種類ごとの設定
with open('app.json', "r", encoding="utf-8") as file:
    confApp = json.load(file)
with open('sim.json', "r", encoding="utf-8") as file:
    confSim = json.load(file)

import cal

FIX_DIGIT = Math.pow(10, 6) #コスト値 int変換桁数(float->int)

class hungarianResult():
    maxSum: float
    maxSum_r: float
    min: float
    Sum: float
    Harmean: float
    combiApTermArray: List[int]

def kumiawase(data, nukitoriNum: float):
    N = len(data)
    arrs : List[List[float]]= []

    if nukitoriNum == 1:
        i = 0
        while i < N:
            arrs.append([data[i]])
            i+=1

    else:
        i = 0
        count = N - nukitoriNum + 1
        while i < count:
            #print(data[(i + 1):])
            ts = kumiawase(data[(i + 1):], nukitoriNum - 1)
            for t in ts:
                t.insert(0,data[i])
                arrs.append(t)
            i+=1
    return arrs


def makeCombiApTerm(terms: List[Term], aps: List[Ap]):
    n = len(terms) + len(aps) - 1
    r = len(aps) - 1
    flg_over_num_ap: bool = True
    data = np.empty(n, dtype=float)
    division : List[List[float]]= [[]]

    count_ap= np.zeros(len(aps))
    combi_ap_term: List = []
    combi_ap_term_tmp: List= [] # 全組み合わせ

    # 初期配列の準備
    for i in range(len(aps)) :
        count_ap[i] = 0 # 各基地局（仮想）の接続台数を0
    for i in range(n) :
        data[i] = i

    # 仕切り位置の組み合わせを出力
    # console.log(n, r)
    division = kumiawase(data, r)
    # print(len(division))

    # 基地局組み合わせ洗い出し
    for i in range(len(division)):
        no_divi: int = 0
        no_ap: int = 1
        j: int = 0
        wk_combi_ap_term_tmp: List[float] = []
        wk_combi_ap_term: List[float] = []

        while j < n :
            if no_divi < len(division[i]) and j == division[i][no_divi]:
                # console.log('div[i][no_div]', division[i][no_divi])
                wk_combi_ap_term_tmp.append(0)
                no_ap += 1
                no_divi += 1
            else:
                wk_combi_ap_term_tmp.append(no_ap)

            j += 1

        k = 0
        j = 0
        while j < n:
            if wk_combi_ap_term_tmp[j] != 0:
                wk_combi_ap_term.append(wk_combi_ap_term_tmp[j])
                k+=1

            j+=1

        combi_ap_term_tmp.append(wk_combi_ap_term.copy())
        # print(wk_combi_ap_term)
        # print(combi_ap_term_tmp)
    # print(wk_combi_ap_term)


    # with open('kari.csv', mode='w', newline='', encoding='utf-8') as file:
    #     writer = csv.writer(file)
    #     for row in combi_ap_term_tmp:
    #         writer.writerow(row)

    #------------------------------------------------------------------------------

    '''for i in range(len(combi_ap_term_tmp)):
        num_ap_accsess: List= [] # 【検討】基地局接続台数

        # AP毎の収容数をカウント
        for j in range(len(terms)):
            count_ap[combi_ap_term_tmp[i][j] - 1] += 1

        # 1つでも収容数を超えているAPは不採用
        for j in range(len(aps)):
            if count_ap[j] > aps[j].termCapa:
                flg_over_num_ap = False

        # 採用した組み合わせのみをリストに追加
        if flg_over_num_ap == True:
            combi_ap_term.append(combi_ap_term_tmp[i])
            # print(combi_ap_term_tmp[i])
            num_ap_accsess.append(count_ap) # 【検討】接続台数カウント
            # print("接続台数："+ str(num_ap_accsess))


        # フラグを初期化
        flg_over_num_ap = True

        for j in range(len(aps)):
            count_ap[j] = 0
    '''


    # print(combi_ap_term)
    # print(len(combi_ap_term))
    return combi_ap_term_tmp


def call_hungarian(terms: List[Term], aps: List[Ap],
                   init_rtt: List[float] = None, erlang_n: List[int] = None):
    hungarianResultAll :List[hungarianResult] = []
    
    #munkeres-------------------------------------------------------------------------------
    # hunres: List = []

    # 行: 組み合わせパターン, 列: 各端末端末
    COMBI_AP_TERM = makeCombiApTerm(terms, aps)
    # print(len(COMBI_AP_TERM))

    #ハンガリアン法計算用の仮想端末と基地局を生成(インスタンスをコピー) */
    APS_VIRTUAL: List[Ap] = aps
    TERMS_VIRTUAL: List[Term] = terms
    costMatrix = np.zeros((len(TERMS_VIRTUAL), len(TERMS_VIRTUAL)))
    costMatrixAll = []
    
    for i in range(len(COMBI_AP_TERM)):
        # 対象の全組み合わせ（パターン）でハンガリアン法実行
        #接続時RTT, 接続時TPを算出
        # cal.calLink(termsVirtual, apsVirtual)
        """
        ハンガリアン法に引き渡すコスト行列を生成
          行: 基地局リソース, 列: 端末, 値: 端末満足度
          正方行列
        """
        # print("-------------------------------------------------------------")
        # print("入力の組み合わせ", COMBI_AP_TERM[i])
        for j in range(len(terms)) : # 基地局リソース（=端末数）
            # print(TERMS_VIRTUAL[j].appNum)
            for l in range(len(terms)):
                if COMBI_AP_TERM[i][l] == 1:
                    TERMS_VIRTUAL[l].setSwitchAp(0)
                elif COMBI_AP_TERM[i][l] == 2:
                    TERMS_VIRTUAL[l].setSwitchAp(1)
                else:
                    TERMS_VIRTUAL[l].setSwitchAp(2)
            # 接続時RTT & 接続時TP計算
            # cal.sumTermAp(TERMS_VIRTUAL, APS_VIRTUAL)
            cal.calLink(TERMS_VIRTUAL, APS_VIRTUAL, confSim["appUseSec"], init_rtt, erlang_n)

            for k in range(len(terms)): # 端末数

                # 組み合わせパターンからデータ構造変換 (=接続k切り替え)
                distAp = COMBI_AP_TERM[i][j] - 1 # 基地局番号1を index 0 にする
                # print(distAp+1)
                TERMS_VIRTUAL[k].setSwitchAp(distAp)

                # 端末満足度計算
                # print((TERMS_VIRTUAL[k].apBssid, APS_VIRTUAL[TERMS_VIRTUAL[k].apBssid].tp))
                satis = cal.calSatisTerm_a(TERMS_VIRTUAL[k], APS_VIRTUAL)
                # print(satis)
                costMatrix[j][k] = round(satis, 6)
                # costMatrix[j][k] = satis

            # 基地局接続台数算出
            # cal.sumTermAp(TERMS_VIRTUAL, APS_VIRTUAL)
        # print(costMatrix)
        #-------------------------------------------------------------------------------print

        """
        組み合わせごとにハンガリアン法を試行
        端末満足度最大の組み合わせを選択・端末満足度の調和平均値を算出
        Object: hungarianResult
        """

        #munkeres-------------------------------------------------------------------------------
        # m = Munkres().compute(copy.copy(costMatrix))
        # asum = sum([costMatrix[idx] for idx in m])
        # hunres.append(asum)
        # print("モジュの満足度合計", hunres[i])
        # print("モジュの結果座標", m)

        HUNGARIAN_RESULT: hungarianResult = hungarian(costMatrix, COMBI_AP_TERM[i])
        # print(HUNGARIAN_RESULT)
        hungarianResultAll.append(HUNGARIAN_RESULT)

        costMatrixAll.append(costMatrix)

        # print(HUNGARIAN_RESULT.max, HUNGARIAN_RESULT.combiApTermArray, "\n")
    # print("-------------------------------------------------------------")
    #-------------------------------------------------------------------------------print

    # 端末満足度最大かつ最小値最大の組み合わせを選択

    #munkeres-------------------------------------------------------------------------------
    # hunres_max: float = 0
    # c = 0
    # for bk in hunres:
    #     c += 1
    #     if bk > hunres_max:
    #         hunres_max = bk
    #         d = c
    # print(hunres_max, d)
    

    # 最大満足度（maxSum）を持つ組み合わせを選択
    # HUNGARIAN_MAX_VALUE = max(result.maxSum for result in hungarianResultAll)


    # 最大値をとる巡目（インデックス）を取得 検証用
    index_of_max = max(enumerate(hungarianResultAll), key=lambda x: x[1].Harmean)[0]
    # 最大値自体を取得（これは上記コードと同様）
    HUNGARIAN_MAX_VALUE = hungarianResultAll[index_of_max].Harmean


    # HUNGARIAN_MAX_VALUE :float = 0
    # a = 0
    # for hungarianResultOne in hungarianResultAll:
    #     a += 1
    #     if hungarianResultOne.maxSum > HUNGARIAN_MAX_VALUE:
    #         HUNGARIAN_MAX_VALUE = hungarianResultOne.maxSum
    #         b = a
    # maxSum 最大を返す
    # print("端末満足度の最大値", HUNGARIAN_MAX_VALUE, "\n選ばれた組み合わせ番号", b)
    print("調和平均の最大値", HUNGARIAN_MAX_VALUE, "最大値の場合の組み合わせ番", index_of_max)
    #-------------------------------------------------------------------------------print
    
    # 最大値であるレコードを抽出

    def filterMax(res, value):
        return res if res.Harmean >= value else None

    # def filterMax(res: hungarianResult, value: float):
    #     if(res.maxSum > value) :
    #         return False
    #     else:
    #         return True

    #resArray : List[hungarianResult] = []
    resArray = list(filter(lambda res:filterMax(res, HUNGARIAN_MAX_VALUE), hungarianResultAll))
    # resArray = list(filter(lambda x:filterMax(x, HUNGARIAN_MAX_VALUE), hungarianResultAll))
    # print(len(resArray))
    # print(resArray)

    # 最小値最大を選択
    res = max(resArray, key=lambda r: r.min)

    # cur = hungarianResultAll[0].min
    # for res in resArray:
    #     if res.min < cur:
    #         RES = res
    

    #接続先切り替え
    for i in range(len(terms)):
        terms[i].setSwitchAp(res.combiApTermArray[i] - 1)
        # print(terms[i].apBssid + 1)
        # print(str(res.combiApTermArray[i]-1))
        # print(str(i)+ 'Dist AP'+ str(res.combiApTermArray[i]-1))
    
    res.combiApTermArray = res.combiApTermArray - 1
    print(f'割り当て後の基地局: {res.combiApTermArray}')
    
    # console.log(res)
    return res

"""
  ハンガリアン法 (scipy.optimize.linear_sum_assignment 使用)
  input: コスト行列（満足度: 大きいほど良い）, 組み合わせパターン
  output: hungarianResult（調和平均, 合計, 最小値, AP割り当て配列）
"""
def hungarian(costMatrix, combi_ap_term: List[List[float]]):
    N: int = len(costMatrix[0])

    # scipy は最小化問題のため, 最大化には符号を反転して渡す
    row_ind, col_ind = linear_sum_assignment(-costMatrix)

    # マッチング結果から満足度リストを取得
    satis_list = [costMatrix[row_ind[i]][col_ind[i]] for i in range(N)]

    # 各端末 (列) に対応するAP番号を格納
    wk_solution_station = np.full(N, -1, dtype=int)
    for row, col in zip(row_ind, col_ind):
        wk_solution_station[col] = combi_ap_term[row]

    satis_sum = sum(satis_list)
    inv_sum   = sum(1 / round(s, 6) for s in satis_list)

    RESULT = hungarianResult()
    RESULT.Harmean          = N / round(inv_sum, 6)
    RESULT.maxSum_r         = satis_sum / N
    RESULT.Sum              = satis_sum
    RESULT.min              = min(satis_list)
    RESULT.combiApTermArray = wk_solution_station

    return RESULT