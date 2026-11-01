# 模型接入流程

地址：`http://服务地址:28080/yunfollow/v1`。请求使用`Authorization: Bearer <API_KEY>`。

模型只持有建议密钥（旧配置键`FNOS_PROPOSAL_KEY`），不要给模型执行确认密钥。执行确认由用户在Bot、已登录网页或受控调用方完成（旧键`FNOS_OWNER_KEY`）。密钥不能相同，不能写入提示词、日志或Git。

## 规定顺序

1. 用户提出观看需求：`POST /drafts`，正文`{"text":"胆大党，2024，第一季","preferences":{"min_resolution":"1080p","subtitles":"zh","replace_existing":false}}`。
2. 只展示返回的作品、年份和季数，不编造ID。无候选时询问片名／年份；不能搜索到同名电影就当剧集。
3. 等用户明确确认后，受控调用方用执行确认密钥请求`POST /drafts/{id}/confirm`，正文`{"tmdbid":240411,"season":1}`。确认不代表已开始下载。
4. 受控调用方`POST /search`安排搜索；用建议密钥`GET /status`查看真实搜索轮次，`GET /candidates`获取最多3个已核验115候选。
5. 展示实际集数、缺集、字幕、分辨率及文件大小。未知项明确写未知；失效及无法核验的分享不能推荐。
6. 用户选择后，受控调用方`POST /candidates/{id}/select`。服务会重新核验，失效、文件清单变化或用户已忽略时拒绝执行。
7. 用户要求下一批时，受控调用方`POST /batches/{id}/next`，只忽略本批资源版本，不取消订阅。
8. `GET /jobs`及`GET /jobs/{task_id}`轮询各阶段。只有真实回执成功才报告成功；Emby刷新接受与实际索引分开。
9. `GET /subscriptions`读取订阅。已播缺集见网页订阅页；作品未收齐时不得声称全集完成。API超时先查询原任务，不重复选择或提交。

## 115离线

建议密钥`POST /offline`，正文`{"uri":"magnet:?xt=urn:btih:...","subscription_id":4}`只创建草稿。用户确认后由受控调用方`POST /offline/{id}/confirm`。`GET /offline`观察115返回的进度。

提交结果未知时只读核验，不盲目重新提交。115离线完成仍需在网页或Bot确认实际文件属于目标作品；未识别的季集不自动整理。离线来源不等同于115分享，不能将其他网盘链接当离线链接使用。

## 只读对话

建议密钥可调用`POST /chat`，正文`{"text":"我库里有什么？"}`。支持任务进度、最近保存、Emby全库查询，以及“还有呢”“查一下《作品名》”等接续问题。

返回`text`、`scope`、`total`和分页按钮。`scope=emby`表示查询到Emby实际记录；`scope=yunfollow_only`表示Emby暂时不可用，仅展示云追自己的记录，不能称为全库。最近115转存时间与Emby入库时间分开显示。

对话解析只允许只读查询，不会创建订阅、选择资源、删除、移动或转存。Bot仍限制为已配置的用户私聊。

## 常见状态

`pending`未开始、`running`执行中、`waiting_confirmation`等待确认、`waiting_retry`等待重试、`done`成功、`failed`失败、`skipped`跳过。任务本身另有`received/queued/saving/stored/organizing/need_manual/done`等状态。

不把失败改成成功，不绕过分类确认，不替换已有视频。遇到不可恢复的错误显示原因，交给用户处理。
