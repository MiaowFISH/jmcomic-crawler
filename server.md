# JMComic 下载服务器 API

基础信息
- 服务地址：默认 http://0.0.0.0:8000（可通过 config.yml 的 server.host/server.port 调整）
- 制品静态路径：{static_route}/{album_id}/{artifact_name}.{zip|pdf}，默认 static_route=/artifacts
- 目录结构：
  - 工作区：data/work/{album_id}/...（按章节标题分子目录保存原图）
  - 制品库：data/artifacts/{album_id}/{artifact_name}.{zip|pdf}
  - 元数据：data/work/{album_id}/meta.json（首次完整下载后写入）

身份与安全
- 加密开启且未提供密码时，服务器按配置生成随机密码，并在任务状态中返回 password 字段，同时在 artifacts/{album_id}/{cache_hash}.pwd 保存副本。
- 隐私策略：密码仅用于打包与哈希，不写入日志。

一、配置说明

关键配置位于 config.yml：
- server.host / server.port：服务监听
- server.static_route：制品下载静态路由（默认 /artifacts）
- server.password：随机密码策略（length/charset）
- server.artifact_name.rule：制品命名规则（album_id | short_hash | random | date）
- jm_comic.dir_rule.base_dir：jmcomic 下载基目录（运行时覆盖为 data/work/{album_id}）
- jm_comic.client.postman.meta_data.proxies：默认代理（任务可覆盖）
- jm_comic.client.domain：域名列表（随 impl 不同）

二、接口列表

1. 提交任务
- 方法：POST /tasks
- 请求体（JSON）：
  - album_id: string，JM 本子 ID
  - output_format: "zip" | "pdf"，默认 zip
  - quality: int[1..100]，可选；仅对 PDF 的 JPEG 重编码生效
  - encrypt: bool，是否加密；默认 false
  - password: string，可选；encrypt=true 且未提供时将随机生成
  - compression: int[0..9]，ZIP 压缩等级；默认 6
  - proxy: string，可选；形如 "host:port" 覆盖默认代理
- 返回（JSON）：
  - task_id: string
  - album_id: string
  - status: "queued"|"downloading"|"processing"|"packaging"|"done"|"failed"
  - stage: string，可选，细粒度阶段
  - progress: int，已处理图片数（统计近似）
  - total_images: int，可选，总图片数（下载完成后提供或通过统计补全）
  - duplicate: bool，是否复用既有下载/打包流程
  - metadata: object，包含专辑基础信息（title/author/tags 等）
  - download_url: string，可选；仅在 done 时提供
  - artifact_filename: string，可选；仅在 done 时提供
  - password: string，可选；encrypt=true 时返回
  - error: string，可选；失败时的错误信息
- 语义：
  - 若同 album 正在下载或已下载，将避免重复调用 API，直接等待/进入打包或复用制品缓存。

2. 查询任务状态
- 方法：GET /tasks/{task_id}
- 返回：同提交任务的响应体结构

3. 列出所有任务
- 方法：GET /tasks
- 返回：TaskStatus[] 列表

4. 获取下载链接（可选）
- 方法：GET /tasks/{task_id}/download/{filename}
- 返回：{ "download_url": "/artifacts/{album_id}/{filename}" }
- 说明：download_url 在 GET /tasks/{task_id} 的 done 状态已给出，本接口仅便于客户端二次确认

三、状态机与阶段
- queued → downloading → processing → packaging → done/failed
- stage 字段用于细节标记，例如 "downloading.photo"、"downloading.photo.done"

四、缓存与去重
- 专辑级去重：同 album_id 的下载仅执行一次。首次完整下载后，写入 data/work/{album_id}/meta.json。
- 离线复用：若检测到 meta.json 标记 complete 且本地图片数量满足 page_count，则不再请求 jmcomic API，仅打包。
- 制品缓存键：包含 album_id、output_format、quality、encrypt、compression、password_hash。
- 命名与索引：
  - 文件名基于 server.artifact_name.rule 生成（album_id/short_hash/random/date）
  - artifacts/{album_id}/artifact_index.json 维护 cache_hash → filename 映射，支持非确定性规则的命中

五、元数据与下载布局
- 在下载过程中采集：
  - JmAlbumDetail：album_id、title/name、author、tags、page_count（如果可用）
  - JmImageDetail：img_url 列表
- 写入位置：data/work/{album_id}/meta.json（字段：album_id/title/author/tags/page_count/total_images/images[]/complete）
- 图片落盘：data/work/{album_id}/{photo_title}/...（多章节将存在多个 photo_title 子目录）

六、代理设置
- 默认代理：来自 config.yml 的 jm_comic.client.postman.meta_data.proxies
- 临时覆盖：POST /tasks 中传入 proxy: "host:port"

七、制品与加密
- ZIP：pyzipper AES (WZ_AES, 256bit)，支持 compresslevel 0..9
- PDF：img2pdf 合并，pikepdf 设置用户/所有者密码（允许覆盖原文件）
- 随机密码策略：由 server.password.length 与 server.password.charset 控制
- 密码可在任务状态 password 字段返回；服务器同时在 artifacts/{album_id}/{cache_hash}.pwd 保存副本

八、错误与异常
- 失败状态将出现在 TaskStatus.error 中
- 常见错误：
  - proxy 格式不合法（需 "host:port"）
  - img2pdf 转换失败或未找到图片
  - jmcomic 网络错误/域名不可达（参考日志与 config.yml 域名/代理设置）

九、示例

1) 提交下载任务（生成加密 ZIP，随机密码）

curl -X POST "http://127.0.0.1:8000/tasks" ^
  -H "Content-Type: application/json" ^
  -d "{ \"album_id\": \"1205184\", \"output_format\": \"zip\", \"encrypt\": true }"

响应示例：

{
  "task_id": "a1b2c3...",
  "album_id": "1205184",
  "status": "downloading",
  "stage": "downloading",
  "duplicate": false
}

2) 轮询任务状态直到完成

curl "http://127.0.0.1:8000/tasks/a1b2c3..."

完成示例：

{
  "task_id": "a1b2c3...",
  "album_id": "1205184",
  "status": "done",
  "artifact_filename": "1205184.zip",
  "download_url": "/artifacts/1205184/1205184.zip",
  "password": "R@nd0mPass"  // 若 encrypt=true
}

3) 获取下载链接（可选）

curl "http://127.0.0.1:8000/tasks/a1b2c3.../download/1205184.zip"

返回：

{ "download_url": "/artifacts/1205184/1205184.zip" }

十、注意事项
- cleanup_policy 尚未实现（配置项已预留）
- total_images 的统计在不同版本 jmcomic 上可能存在差异，服务端会在 after_album 时尽量计算并在必要时回退到文件系统统计
- 若使用非确定性命名规则（random/date），建议依赖 artifact_index.json 做缓存命中
- 若域名访问受限，可参考 docs/tutorial/8_pick_domain.md 测试当前可达域名并更新 config.yml

参考
- jmcomic 配置说明：[docs/option_file_syntax.md](docs/option_file_syntax.md)
- 关键实现：[python.def _download_album()](app/main.py:433)、[python.def _get_artifact()](app/main.py:515)、[python.def _package_task()](app/main.py:396)
