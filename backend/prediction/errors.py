# -*- coding: utf-8 -*-
"""预测失败的异常类型。

为什么要跟 `predict_fixture` 的 `return None` 分开：

  return None          = 【不需要预测】：黑名单对阵、库里查无此场。属正常跳过
  raise PredictError   = 【该预测但没跑成】：特征不足 / 联赛没模型 / LLM 没回来

以前一律 `return None`，接口层只能笼统回一句「预测失败（数据不足或比赛不存在）」——
于是「LLM 超时」和「这个联赛根本没模型」在页面上一模一样，都得去翻日志才知道。
分开之后 API 能把原因原样带出去，页面上的失败格子也能写清楚是哪一步。

放在独立模块（不写进 predict.py）是为了让 `app/api/` 能直接 import 接住它：
predict.py 顶部要拖 pandas / xgboost / plotly 一堆重依赖，import 一次很贵。
"""


class PredictError(Exception):
    """单场预测失败。reason 给人看，stage 给日志和排查用。"""

    def __init__(self, reason: str, stage: str = ""):
        super().__init__(reason)
        self.reason = reason    # 一句话原因，会原样显示给用户
        self.stage = stage      # feature / model / llm
