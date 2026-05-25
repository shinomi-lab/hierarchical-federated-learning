import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt #type:ignore
from matplotlib import rcParams
from typing import List
from term import Term # 端末1台が持つデータ構造 

def exportGraph(satisArray: List, presatisArray: List, satisMovingArray: List, term: List[Term]):
    X_LEN = len(satisArray)
    xArray: List = []
    for i1 in range(len(satisArray)):
        xArray.append(i1)
    x2Array = list(range(len(satisMovingArray)))
      
    y1, y2, y3 = satisArray, presatisArray, satisMovingArray
    c1, c2, c3 = "blue", "red", "orange"
    l1, l2, l3 = "satis", "presatis", "satis_moving"

    #Plot Data
    fig, ax1 = plt.subplots(figsize=(10.0, 6.0))
    ax2 = ax1.twinx()
    ax1.set_xlabel("Assignment No.", fontname = "Hiragino Sans", fontsize=18)
    ax1.set_ylabel("Terminal Satisfactory", fontname = "Hiragino Sans", fontsize=18)
    ax1.grid()
    # ax1.set_xlim([0, X_LEN])
    ax1.relim()
    ax1.autoscale()
    ax1.set_ylim([0, 1])
    ax1.fill_between(xArray, y1, color=c1, linestyle="dashed", label = l1, alpha=0.5)
    ax1.plot(xArray, y2, color = c2, linestyle="dashed", label = l2)
    ax1.plot(x2Array, y3, color = c3, label = l3)
    
    # y1の値を表示
    for i, v in enumerate(y1):
        ax1.text(i, v, f'{v:.3f}', color=c1, fontsize=8, ha='center', va='bottom')

    # y2の値を表示
    for i, v in enumerate(y2):
        ax1.text(i, v, f'{v:.5f}', color=c2, fontsize=8, ha='center', va='bottom')

    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1, l1)
    ax1.legend(h1 + h2, l1 + l2)
    # plt.show()  # 非インタラクティブ環境では省略

"""
  Layout
  const layout1: Layout = {
    title: '端末満足度の調和平均',
    xaxis: {
      title: '割当回数',
      showgrid: false,
      zeroline: false
    },
    yaxis: {
      title: '端末満足度の調和平均',
      showline: false,
      rangemode: 'nonnegative',
      range: [0,1]
    }
  }

  const layout2: Layout = {
    title: '通信制限数',
    xaxis: {
      title: '割当回数',
      showgrid: false,
      zeroline: false
    },
    yaxis: {
      title: '通信制限数',
      showline: false,
      rangemode: 'nonnegative'
    }
  }
"""

  # stack([graphData_satis, graphData_satis_moving], layout1)
  # stack([graphData_over], layout2)
  # グラフ1つver

  # const data = [graphData_satis, graphData_over]
  # const layout = [layout1, layout1]
  # plot(data, layout1)

'''
def exportGraphTypeC(satisArray: List, satisMovingArray: List, overTermArray: List, intervalSec: float, term:List[Term]) :
    X_LEN = len(satisArray)
    xArray: List = []
    for i in range(len(satisArray)):
        xArray.append(i)
    x2Array = list(range(len(satisMovingArray)))
    
    y1, y2, y3 = satisArray, satisMovingArray, overTermArray
    c1, c2, c3 = "blue", "orange", "green"
    l1, l2, l3 = "satis", "satis_moving", 'over'

    #Plot Data 
    fig, ax1 = plt.subplots(figsize=(10.0, 6.0))
    ax2 = ax1.twinx()
    ax1.set_xlabel("時間/s", fontname = "Hiragino Sans")
    ax1.set_ylabel("端末満足度の調和平均", fontname = "Hiragino Sans")
    ax2.set_ylabel("通信制限数", fontname = "Hiragino Sans")
    ax1.grid()
    ax1.set_ylim([0, 10])
    ax2.set_ylim([0, len(term)+30])
    ax1.fill_between(xArray, y1, color=c1, linestyle="dashed", label = l1, alpha=0.5)
    ax1.plot(x2Array, y2, color = c2, label = l2)
    ax2.plot(xArray, y3, color = c3, label= l3)
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2)
    # plt.show()  # 非インタラクティブ環境では省略
'''

'''
    #Layout
    const layout_combi: Layout = {
      # title: '端末満足度の調和平均と通信制限数',
      width: 750,
      # showlegend: true, # 凡例の表示有無
      # showlegend: false, # 凡例の表示有無
      legend: { "x": 0.4, "y": 1.3 }, #凡例の位置
      xaxis: {
        title: '時間/s',
        showgrid: true,
        zeroline: false,
        dtick: intervalSec,
      },
      yaxis: {
        title: '端末満足度の調和平均',
        showline: false,
        rangemode: 'nonnegative',
        range: [0,1]
      },
      yaxis2: {
        title: '通信制限数',
        showline: false,
        rangemode: 'nonnegative',
        overlaying: 'y',
        side: 'right',
        range: [0,100]
      }
    }
    # stack([graphData_satis, graphData_satis_moving], layout1)
    # stack([graphData_over], layout2)
    # グラフ1つver
    stack([graphData_satis, graphData_satis_moving, graphData_over], layout_combi)
    plot()

    # const data = [graphData_satis, graphData_over]
    # const layout = [layout1, layout1]
    # plot(data, layout1)
  }


  exports.exportGraph = exportGraph
  exports.exportGraphTypeC = exportGraphTypeC
'''