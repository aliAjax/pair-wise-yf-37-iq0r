# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访；`exposure`：场所暴露活动（病例确认后的登记记录）。

## 场所暴露与随访

病例进入 `confirmed`/`probable`/`recovered` 状态后，调查员可登记场所暴露活动：

```
POST /api/entities/<case_id>/actions
{"action": "register_exposure", "data": {
  "venue": "场所名",
  "window_start": "2026-03-01T18:00",
  "window_end": "2026-03-01T22:00",
  "owner": "investigator-7",
  "due_at": "2026-03-15",
  "contacts": [
    {"person_id": "P-2", "phone": "138...", "contact_detail": "...",
     "exposure_start": "2026-03-01T19:00"}
  ]
}}
```

登记后系统按人同步随访事项（同一来源病例范围内）：

- 新接触者：创建 `contact`，关联来源病例、场所、负责人和到期日，初始为 `identified`。
- 已有开放随访（`identified`/`following`）：只刷新联系方式并关联该活动，**首次暴露时间、负责人、到期日保留**。
- 已完成随访（`completed`）：**不重复生成**，旧记录保持不变。
- 同一名单内同一人出现多次：合并为一条，保留最早暴露时间，联系方式以最后一次为准。

名单更正用 exposure 的 `update_roster` 动作（同样遵循上述同步规则）。病例 `closed` 后，登记新活动和更正名单都会被拒绝；已有的 exposure、contact 记录仍可通过查询接口和审计时间线查看。

## 值班看板

- `GET /api/duty?owner=<负责人>&as_of=<时间>`：按负责人筛选（可空），返回每个场所的未完成随访数、场所内逾期名单，以及全局逾期名单；`as_of` 仅用于回溯演练。
- 浏览器打开 `/duty`：值班页，可输入负责人筛选。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询（`case`/`contact`/`exposure`），可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
