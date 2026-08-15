# 智渔Agent 2.0 评委路演 PPT

文件：`智渔Agent-2.0_GOAI无界应用评委路演.pptx`

## 生成

```bash
uv run --with python-pptx python deliverables/build_goai_ppt.py
```

PPT 为 16:9 格式。流程图、架构图、表格和文字均为可编辑对象；产品页面和摄像头画面使用截图或位图素材。

## 素材来源

- `goai-logo.png`：GOAI 官方网站公开 Logo。
- `goai-home.png`：GOAI 官方网站首页截图，保留作视觉参考素材。
- `fishagent-monitor*.png`、`fishagent-reports*.png`：本地 3000 端口运行实例截图。
- `b01-surface.png`、`b01-underwater.png`：项目内模拟摄像头画面。

## 封版前占位内容

- 团队名称、成员、分工与联系方式
- 真实试点对象、收益指标、失败案例和数据授权说明
- 评委可访问的线上地址、账号、Demo 视频
- 最终仓库、许可证、第三方模型 / API / 依赖清单
- 现场设备接入责任边界与专业人员审核确认

PPT 的“当前工程证据”按照本地项目现状撰写；模拟数据、模拟摄像头和本地 MQTT Broker 的适用边界已在页面中说明。
