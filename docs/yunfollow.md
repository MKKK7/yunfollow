# 云追 YunFollow 集成说明

基于 [Derrors/reelay](https://github.com/Derrors/reelay) v1.4.1，基线提交
`60e1a463412261e37e1065910ba05dc02ae354bc`。保留MIT许可证和上游实现。
维护仓库：https://github.com/MKKK7/yunfollow ，分支 `main`。

## 范围

一个容器、SQLite和既有服务。首版面向115分享中的剧集/番剧，不做视频下载上传中转。
默认最低1080p、中文字幕、不覆盖已有集；未知画质/字幕需用户确认。

0.2.3删除阿里云盘、夸克发现和推送模块。盘搜只接受115；其他网盘不采集、不核验、不推送。

```text
确认TMDB剧集与季数
 -> 搜索已配置的来源
 -> 只读核验115有效性和实际季集文件
 -> 核对剧集类型/TMDB身份
 -> 仅合格资源进入任务
 -> 115原生转存和云端整理
 -> 调用LitePan生成STRM、刮削
 -> 调用现有元数据上传能力并SHA256读回核验
 -> 调用LitePan已有Emby连接刷新媒体库
```

失效、电影、错误作品不进入待确认列表，只记录无链接的排除统计。
网络故障、来源不可读不冒充“没有资源”。已验证的分享会重新读取文件清单，
同一分享新增一集也能形成新任务；原项目按已入库季集去重。

## 机器人操作

先通过官方 [@BotFather](https://t.me/BotFather) 创建机器人。管理台填写Bot Token，
`integration.env`填写`FNOS_TELEGRAM_OWNER_ID`，只服务该数字用户的私聊。
这里使用Bot API，不需要个人Telegram API ID/API Hash。

| 操作 | 命令 |
|---|---|
| 订阅 | 发送剧名和年份，核对作品、季数后点击确认 |
| 调整偏好 | `/watch 720p any 剧名`（不限字幕）；`/watch 1080p zh 剧名` |
| 添加频道/文档 | `/source add https://t.me/CHANNEL` 或 `/source add https://www.kdocs.cn/l/ID` |
| 添加盘搜 | `/source add pansou http://NAS_IP:8085` |
| 查看/移除来源 | `/sources`；`/source remove 来源ID` |
| 立即搜索 | `/search`，只搜索已确认的订阅 |
| 真实状态 | `/status`；入库集数不是下载进度 |
| 排除原因 | `/audit`，不展示已失效链接 |
| 联动阶段 | `/job 任务ID`，包含服务执行回执 |
| 检查现有服务 | `/integrations`，只读连通检查，不宣称已处理媒体 |

Telegram暂只支持公开频道，通过公开历史搜索页按片名查询，每个来源/片名有读取上限。
不登录个人Telegram账号。

## 文档持续同步

0.2.1开始在容器内运行官方Linux版`kdocs-cli`，使用固定版本和SHA256校验。
每15分钟独立读取登记的文档，新增加文档也会触发同步，不需要电脑在线。
正文只在内存处理；仅保存分享候选、短标题、内容哈希和完整性标记。

CLI凭据通过官方`auth login`或用户明确授权的迁移导入；它在Linux容器中使用官方
加密文件后端，持久卷挂载到`/home/reelay/.config/kdocs-cli`，目录权限700、文件600。
重建容器不会删除授权。账号令牌到期或被撤销时仍需重新授权，不能保证永久有效。
首次独立授权可在终端执行`docker exec -it yunfollow kdocs-cli auth login`，
自行完成输出链接的授权；不要把令牌输出到日志或提交到仓库。

以下电脑桥接仅作为可选兼容方式；已迁移到容器的部署应停用电脑计划任务。

桥接可在已有授权的Windows电脑运行，每15分钟一次。需要该电脑在线并保持对应用户登录。
超过30分钟没有新同步时，容器将来源标成过期，不把旧快照当成已刷新。
私有桥接配置放在仓库外，例如：

```json
{"baseUrl":"http://NAS_IP:28080","key":"DEDICATED_SOURCE_KEY","cli":"ABSOLUTE_PATH_TO_KDOCS_CLI","statusFile":"PRIVATE_STATUS_PATH"}
```

```text
node fnos-adapters/kdocs-bridge.mjs ABSOLUTE_PATH_TO_PRIVATE_CONFIG
```

容器`secrets.json`中的`fnos_source_key`须与桥接密钥相同。这个专用Key只能读取来源和
提交已登记文档的候选，不能新增来源、确认订阅或触发转存。API不会保存或返回账号凭据。
Windows可用任务计划程序按当前登录用户运行该命令。停止桥接时删除对应计划任务即可。

## 部署

```text
docker build -f Dockerfile.yunfollow -t yunfollow:0.3.1 .
docker compose -f compose.yunfollow.yml up -d
```

Compose遵循fnOS常见绝对目录：

```text
/vol1/1000/Docker Compose/manage/YunFollow/docker-compose.yml
/vol1/1000/AppData/YunFollow/data
/vol1/1000/AppData/YunFollow/integration.env
```

复制`integration.env.example`并填写服务地址、LitePan凭据及OpenList上传凭据。
Compose项目的`.env`可设置`YUNFOLLOW_BIND_ADDRESS=NAS_IP`；默认只绑定127.0.0.1。
首次部署须显式设置`FNOS_SAVE_CID`（115媒体库根目录）、`LITEPAN_STRM_TASK_ID`、
`LITEPAN_EMBY_CONFIG_ID`、`LITEPAN_EMBY_LIBRARIES`（分类到媒体库ID的JSON映射）。
`init_yunfollow.py`只创建首次配置，已有配置不会被覆盖。

STRM目录以只读方式挂入；不挂Docker Socket、不使用privileged。
元数据只允许NFO/图片，10MiB/文件、100MiB/作品、最多100文件。
保留LitePan全局同步设置；当它未启用双向回写时调用已有OpenList PUT接口。
Emby Key由LitePan持有，本容器只传现有连接ID、媒体库ID。

管理台首次打开设置密码。模型支持OpenAI兼容接口和`/anthropic` Messages接口。
MiniMax若不支持联合类型Schema，应在默认规则选择`json_object`；本地仍校验完整结构。

## 内置API与外部AI流程

`/api/docs`是登录后的OpenAPI。外部模型只持有`FNOS_PROPOSAL_KEY`：

1. `GET /fnos/v1/status`核对模型、来源状态和真实搜索进度。
2. `POST /fnos/v1/drafts`，例如`{"text":"剧名 年份 第1季"}`。
3. 向用户展示返回的TMDB作品、年份、季数和偏好，等待用户确认。
4. 只有受信任确认端使用独立`FNOS_OWNER_KEY`调用
   `POST /fnos/v1/drafts/{id}/confirm`，正文为`{"tmdbid":123,"season":1}`。
   模型不得持有这个Key。机器人按钮使用已配置的用户身份完成相同确认。
5. `GET /fnos/v1/jobs`和`/jobs/{id}`读取阶段/回执；`GET /audit`读取排除原因。
   没有合格资源就等待，不得声称已经转存或已完成联动。

来源API：`GET/POST /fnos/v1/sources`、`DELETE /sources/{id}`；写操作要求Owner Key。
`POST /search`触发立即搜索；`GET /integrations`返回只读检查和标明用途的隔离验收记录。
`POST /integrations/check`执行只读检查。
新任务必须携带服务端预核验记录，不能靠来源文本或模型自行伪造“已核验”。

## 检查

```text
python -m unittest tests.test_fnos tests.test_fnos_sources -v
node --test tests/fnos_adapters.test.mjs
python -m reelay.main --dry-run
```

模拟测试、连通检查、隔离媒体验收与正式订阅任务是不同证据；不能互相替代。
正式订阅无合格资源时，其后续阶段应保持未执行。
