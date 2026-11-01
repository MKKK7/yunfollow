# 云追 · YunFollow

面向115的云端追更与媒体整理。通过网页或Telegram确认作品和资源，调用现有盘搜、LitePan、OpenList和Emby，保留每一步的实际回执。

- **仅115**：分享原生转存，或将磁力／电驴交给115云端离线。不在NAS下载视频后上传。
- **每批三个候选**：核验链接与实际季集，展示字幕、画质、缺集和逐集大小；支持忽略与下一批。
- **完整任务流程**：全部阶段、调用服务、进度、错误和阶段重试。
- **分类待处理**：电影与剧集分开，动漫优先于地区；不明确时进入115待分类目录，确认后继续。
- **来源管理**：Telegram公开频道、金山文档、盘搜；名称、备注、启停、间隔及读取测试。
- **持续补集**：以实际已收集和TMDB已播集数计算缺集，满足偏好的新增缺集自动补充。
- **Telegram卡片**：资源选择、忽略、下一批、目录选择、任务重试；自然语言意图经本地规则校验。
- **服务复用**：LitePan生成STRM和刮削；现有OpenList回写NFO/图片并SHA256核验；LitePan通知Emby，再单独核验索引。

## 界面入口

`/workflow/tasks` 任务流程 · `/workflow/pending` 待处理 · `/workflow/sources` 来源管理 · `/workflow/integrations` 服务联动 · `/workflow/subscriptions` 订阅与补集 · `/workflow/rules` 命名与分类。

接口调用成功不代表资源已收齐。任务完成、已播集数收齐和持续追更分别展示；资源不存在时持续等待，不承诺一定找到。

## 构建和部署

```sh
cd web
npm ci
npm run build
cd ..
docker build -f Dockerfile.yunfollow -t yunfollow:0.3.0 .
docker compose -f compose.yunfollow.yml up -d
```

Compose使用`/vol1/1000/AppData/YunFollow/`保存配置、数据库和文档CLI授权；部署前填写环境模板与已有服务连接。容器不需要Docker socket或特权模式。默认只绑定本机，通过`YUNFOLLOW_BIND_ADDRESS`选择局域网地址。

首次部署需要明确设置115库根目录和服务连接；不会自动创建订阅。旧版配置键和Python包名保留兼容，以便迁移现有数据，它们不是产品名称。

## 接口与操作说明

详见[模型接入流程](docs/agent-api.md)、[集成说明](docs/yunfollow.md)和[实施与验收计划](docs/implementation-plan.md)。

## 来源与许可证

本项目基于[Derrors/reelay](https://github.com/Derrors/reelay) v1.4.1维护，基线提交`60e1a463412261e37e1065910ba05dc02ae354bc`，保留上游MIT许可证。金山文档使用官方CLI。云追新增代码同样使用MIT许可证。