# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件；`plate`：实验室检测板。

## 检测板流程

检测板（`plate`）解决复测时同一位置容易放错、阳性无法落到具体观察的问题：

1. 由 `lab`/`admin` 从**已送检（`in_lab`）**样本排样创建检测板（状态 `pending`）。每个孔位形如 `A1`；对照孔用 `type:"control"` 单独标注 `control:"positive"|"negative"`，样本孔为 `type:"sample"` 并携带 `sample_id`。
2. 创建时拦截：板号 `plate_code` 重复、孔位重复、同板内同一样本重复排样、样本已在另一块**未完成（pending）**板上、样本未送检。
3. 通过 `set_well_result` 逐孔录入 `positive`/`negative`。
4. `confirm` 仅在整板结果齐全、至少一个对照孔且所有对照孔结果与标注一致时通过；页面与 `data.summary` 提供孔位、缺失结果（`missing`）和阻塞原因（`blockers`：`missing_results`/`missing_controls`/`invalid_controls`）。
5. 确认后级联处理：**阳性**样本转入 `resulted` 并写回 `plate_code`/`plate_id`/`well`（随后可 `retest` 复测，复测排上新板）；**阴性**样本自动 `close`。
6. 对已确认的板再次 `confirm` 为幂等操作，原样返回、沿用第一次结果，不产生新版本或审计。

创建请求示例：

```json
POST /api/plates
{"plate_code": "P-001", "wells": [
  {"well": "A1", "type": "control", "control": "negative"},
  {"well": "A2", "type": "sample", "sample_id": "<sample-id>"},
  {"well": "B1", "type": "control", "control": "positive"}
]}
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
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

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
