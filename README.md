# JMComic 下载服务器

本项目提供一个基于 FastAPI 的服务，使用 jmcomic 库下载整本漫画并打包为 zip 或 pdf，支持加密、质量、压缩、代理、任务轮询与缓存。

API 详解：[server.md](server.md)

功能特性
- 按 album_id 下载整本漫画，输出 zip 或 pdf
- 可配置压缩等级、PDF 图像质量、是否加密与密码
- 任务队列与轮询：提交任务返回 task_id，查询进度与结果链接
- 制品缓存：参数一致时直接命中缓存，避免重复打包
- 专辑级去重：同 album_id 只下载一次；重复任务仅打包或直接返回缓存
- 代理支持：传入 host:port 或使用配置默认代理
- 不硬编码参数：通过 YAML 配置文件管理
- 自定义下载器回调：在下载前采集元数据（作者、章节数、标签、图片 URL 等）写入 meta.json

环境要求
- Python 3.9+（建议 3.11）
- 依赖库：fastapi、uvicorn、pyzipper、img2pdf、pikepdf、Pillow、PyYAML、jmcomic

安装
- 使用 pip：

pip install -r requirements.txt

如未提供 requirements.txt，可逐个安装：

pip install fastapi uvicorn pyzipper img2pdf pikepdf Pillow PyYAML

安装 jmcomic（若需）：

pip install jmcomic

运行
- 方式一：使用 uvicorn

uvicorn app.main:app --host 0.0.0.0 --port 8000

- 方式二：通过启动函数

python -c "from app.main import start; start()"

配置
- 编辑 [config.yml](config.yml)：
```
server:
  host: 0.0.0.0
  port: 8000
  data_dir: "./data"
  static_route: "/artifacts"
  password:
    length: 12
    charset: "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#-_"
  artifact_name:
    rule: short_hash  # 可选 album_id | short_hash | random | date
    short_hash:
      length: 8

jm_comic:
  dir_rule:
    base_dir: "downloads/"
  client:
    impl: html
    retry_times: 1
    postman:
      meta_data:
        proxies: null
  domain:
    html:
      - "jm18c-cvb.net"
```
目录结构
- data/work/{album_id}/...：按章节标题分子目录保存原图与 [meta.json](data/work)
- data/artifacts/{album_id}/{artifact_name}.zip|pdf：打包制品
- static 路由：默认 /artifacts，指向 data/artifacts

快速上手
1) 提交下载任务（加密 ZIP，随机密码）
```
curl -X POST "http://127.0.0.1:8000/tasks" ^
  -H "Content-Type: application/json" ^
  -d "{ \"album_id\": \"1205184\", \"output_format\": \"zip\", \"encrypt\": true }"
```
响应包含 task_id；随后轮询：
```
curl "http://127.0.0.1:8000/tasks/{task_id}"
```
完成后返回：
- download_url: "/artifacts/{album_id}/{artifact_name}.zip"
- artifact_filename: "{artifact_name}.zip"
- password: 随机密码（若 encrypt=true）

2) 生成 PDF，重编码 JPEG 质量
```
curl -X POST "http://127.0.0.1:8000/tasks" ^
  -H "Content-Type: application/json" ^
  -d "{ \"album_id\": \"1205184\", \"output_format\": \"pdf\", \"quality\": 85 }"
```
3) 指定代理（覆盖默认）
```
curl -X POST "http://127.0.0.1:8000/tasks" ^
  -H "Content-Type: application/json" ^
  -d "{ \"album_id\": \"1205184\", \"proxy\": \"127.0.0.1:7890\" }"
```
API 说明
- 详见 [server.md](erver.md)
- 端点概览：
  - POST /tasks → 提交任务
  - GET /tasks/{task_id} → 查询任务
  - GET /tasks → 列出任务
  - GET /tasks/{task_id}/download/{filename} → 获取下载链接

缓存与去重
- 首次完整下载后写入 meta.json 并标记 complete
- 后续重复任务仅进行完整性检查与打包，不再触发 jmcomic API
- 制品命名规则由 server.artifact_name.rule 控制，缓存索引维护在 artifact_index.json

加密与密码
- ZIP：pyzipper AES (WZ_AES, 256bit)，compression 0..9
- PDF：img2pdf 合并，pikepdf 加密（允许覆盖原文件）
- 随机密码：由 server.password 控制，策略读取与生成

代理设置
- 默认代理来自 [config.yml](config.yml) jm_comic.client.postman.meta_data.proxies
- 临时覆盖：POST /tasks 中传入 "host:port"

元数据与下载布局
- 下载器回调采集：
  - JmAlbumDetail：album_id/name/page_count
  - JmImageDetail：img_url
- 图片路径：work/{album_id}/{photo_title}/...

注意事项
- cleanup_policy 暂未实现
- total_images 统计在不同 jmcomic 版本下可能差异，已在 after_album 时尽量补全
- 域名不可达或需登录的专辑，请检查 config 域名与 cookies（参见 jmcomic 文档）

致谢
- jmcomic 项目及其文档
- FastAPI 与相关开源库
