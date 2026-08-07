# Babel 方案:中英会议实时双语字幕

[English version](README.md)

管线:麦克风/系统声音 → 火山 Seed-ASR(流式、热词、说话人聚类)→ 火山机器翻译大模型(matx_translate,术语表原生注入)→ 浏览器字幕页。**一把 API Key 覆盖全部服务**:识别和翻译同在火山语音产品线(openspeech.bytedance.com),共用 `VOLC_ASR_API_KEY`。

- 英文发言 → 显示中文翻译;中文发言(含中英混说)→ 显示英文翻译
- 句子没说完时,原文活行和"≈ 快速翻译"草稿持续更新;句号、说话人切换或 3 秒静音后,精翻覆盖

## 工作原理

`asr_client.py` 实现 Seed-ASR WebSocket 二进制协议(优化版双向流式 bigmodel_async),开启二遍识别:中间结果快,判停片段由非流式模型重识别保证准。热词双通道:直传 100 token(放平台规则不允许进词表的词加英文专名),其余走自学习平台词表。纠错映射(glossary.json 的 corrections)在上屏和送译前修正固定误识(MATE→Matt、CKC→CKCon、RGB 加加→RGB++)。

`main.py` 的句子累积器:片段攒成活行,按句号、200 字上限、3 秒静音、语言边界(长英文段 vs 含中文段)或说话人切换结算。翻译默认 volc-mt 后端,实测每句 0.2-0.9 秒;术语表按方向翻转,大小写变体自动生成,英译中译值用中英混合形态("Fiber网络")强制效果最好。备用后端:ark(方舟豆包,需 ARK_API_KEY,必须关 thinking)和 qwen-mt(阿里)。

## 配置

1. 语音技术控制台(console.volcengine.com/speech):开通**豆包流式语音识别 2.0(小时版)**和**机器翻译大模型**(volc.speech.mt),创建 API Key。
2. `../.env` 写 `VOLC_ASR_API_KEY=...`(程序自动加载);可选 `VOLC_BOOSTING_TABLE_ID`(自学习平台热词表,上传 ../hotwords/boosting_table.txt 后获得)。
3. 安装:`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`(PyPI 慢加清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`)。

## 日常使用

线上会议一次性音频配置(macOS 音频 MIDI 设置):**多输出设备**勾扬声器加 BlackHole 2ch(你听得到、程序拿一份);**聚合设备**勾 BlackHole 2ch 加你的麦克风(远端加本地声音都进字幕)。会议软件输出选多输出设备。

每次开会:双击 `../Babel.command`(设备序号写死在里面),或手动:

```bash
cd CKBA/Babel/solution
.venv/bin/python main.py --device <聚合设备序号> --channels 4
.venv/bin/python main.py --list-devices    # 查序号
.venv/bin/python main.py --share           # 打印 LAN 链接;装了 cloudflared 自动出公网链接
```

启动后浏览器自动打开字幕页:说明按钮看界面解释,分享按钮复制链接,导出按钮下载 md(原文/译文/双语,说话人可命名且浏览器记忆)。

## 成本(2026-08,以控制台为准)

- ASR:后付费 ¥3.5/小时(约 $0.49);30 小时资源包 ¥28(约 $3.9),折合 ¥0.93/小时(约 $0.13)。
- 翻译:¥1.62/百万 token(约 $0.23),一会议小时通常不到 10 万 token,可忽略。
- 合计约 ¥3.5/小时(约 $0.49);对照讯飞同传 LLM 档 ¥4080/100 小时(约 $570),即 ¥40.8/小时(约 $5.7),便宜约 12 倍。

## 稳健性说明(2026-08-07 审计后)

- ASR 断线自动重连(退避 2 到 10 秒),断线期间音频队列丢最旧块并告警,UI 顶栏显示重连状态、恢复后回到 connected;鉴权类错误(key 错、服务未开通、握手 401/403)直接报错退出,不会无限重试。
- 翻译失败自动重试一次,仍失败显示"⚠ 翻译失败 translation unavailable",失败结果不进缓存。
- 每个确定句自动追加到 `transcripts/babel-<时间戳>.jsonl`(经 to_thread,OneDrive 卡顿不阻塞主循环),退出时渲染同名 .md,Ctrl-C 也会收尾。
- 浏览器断线重连按句子 id 幂等;历史重放走 await 直发,任意长度的历史都不会把晚加入者卡死。
- `--share` 下页面挂在随机 token 路径下;cloudflared 隧道后台启动,不阻塞 ASR,公网链接就绪后自动出现在分享弹窗;卡死的观看端会被断开而不拖慢管线。
- `--device` 支持设备名子串(如 `--device Aggregate`),不再受序号漂移影响。
- 草稿翻译固定 0.5 秒防抖,且用只含恒等术语(专名)的精简术语表,成本大幅下降而刷新不变慢。
- 语言切分正则有回归断言:`python test_split.py`。
- 已知限制:没有暂停按钮(服务端 8 秒空闲即断连,真暂停需要和重连联动,后续再做)。
