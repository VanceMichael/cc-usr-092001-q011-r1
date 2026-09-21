# 运河纠纷程序衔接引擎

本项目用于管理海事争议、程序移送与期限中的稳定事实和交换边界。仓库提供基础服务、数据库初始化入口、领域说明和脱敏示例，便于八家共建单位（海事、检察、司法行政、人社、调解、仲裁、法院等）在一致约定下协作。

## 目录

- `contracts/` 保存外部交换字段示例（事件、移送、公众进度）。
- `docs/` 说明领域对象、时间和标识约定。
- `src/` 保存服务代码：`schema.py` 表结构、`store.py` 数据访问、`domain.py` 领域规则、`app.py` HTTP 接口。
- `scripts/` 保存数据库初始化入口。
- `tests/` 保存基础行为检查。

## 运行

执行 `make test` 检查基础行为，执行 `make migrate` 初始化本地数据目录，执行 `make run` 启动服务。默认监听 `8080` 端口，健康检查地址为 `/health`。

配置通过环境变量传入（`PORT`、`DATABASE_PATH`），敏感值和本地数据库文件不得提交到仓库。

## 接口概览

请求经请求头标识行为主体：`X-Actor-Ref`（主体引用）、`X-Actor-Role`（`registrar`/`unit`/`public`/`supervisor`）、`X-Actor-Unit`（单位类别，单位角色必填）。

- `POST /parties` 登记当事方最小身份资料。
- `POST /cases` 登记案件：管辖连接点、请求事项、证据封存摘要、法定期限；自动进行重复立案识别（只标记不合并）。
- `POST /cases/{ref}/accept` 受理并生成可解释程序路径；选择不可行时返回理由与替代建议。
- `GET /cases/{ref}/path` 查询全部路径版本（旧路径保留）。
- `POST /cases/{ref}/transfers`、`POST /transfers/{ref}/confirm` 移送发起与交出/接收双向确认。
- `POST /cases/{ref}/actions` 撤回、部分和解、管辖异议、跨境送达（均保留旧路径）。
- `POST /duplicates/{ref}/decision` 重复立案标记的人工确认或排除。
- `GET /cases/{ref}/progress` 公众端本人案件进度；`GET /cases/{ref}/materials` 协同单位授权材料；`GET /cases/{ref}/supervision` 监督视图（停留时间、效力状态、下一责任方）。
- `POST /events` 接收外部交换事件，保留来源发生时间，`event_id` 幂等。
