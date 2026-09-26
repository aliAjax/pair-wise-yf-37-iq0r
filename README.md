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

- `case`：病例和调查状态；`contact`：接触者随访；`venue`：场所暴露活动。

## 场所暴露随访

- 病例确认（`confirmed`/`probable`/`recovered`）后才能登记场所，记录地点、暴露时段和负责人。
- `POST /api/venues/<id>/attendees` 登记接触者，系统按人生成 `contact` 随访事项，自动关联来源病例、负责人和到期日（默认暴露结束 +14 天，可用`due_at`覆盖）。
- 同一人重复参加同一活动：只更新联系方式，首次暴露时间保留；随访已完成的不再生成。
- 病例关闭（`closed`）后停止新增场所和随访事项，旧记录仍可查询。
- `GET /api/duty?owner_id=<负责人>` 值班页：按负责人汇总各场所未完成数和逾期名单。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/venues/<id>/attendees`：登记场所接触者，幂等生成随访事项。
- `GET /api/venues/<id>/attendees`：查看场所接触者名单。
- `GET /api/duty`：值班页汇总，可用`?owner_id=`按负责人筛选。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
