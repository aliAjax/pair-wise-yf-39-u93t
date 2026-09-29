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

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件；`plate`：检测板（孔位排样与整板确认）。

## 检测板流程

样本逐个送检（`sample.send_lab` → `in_lab`）后，实验室在检测板上排样复测：

1. `POST /api/plates`（`lab`/`admin`）：用唯一`plate_code`建空板，默认 8×12，可用`rows`/`cols`调整（1..16 行、1..24 列）。板号重复返回 409。
2. `add_wells`：把已送检（`in_lab`）样本放入孔位，或用`{"control":"positive"|"negative"}`单独标注对照孔。以下情况会被拦住（409/400）：
   - 孔位已被其它样本/对照占用；
   - 同一样本同时出现在两块未完成（`draft`/`testing`）板上；
   - 样本不存在或不在`in_lab`状态。
3. `start_testing`：`draft` → `testing`，之后逐孔`record_results`录入`positive`/`negative`。
4. `GET /api/plates/<id>/readiness`：返回孔位、`missing_results`和`blockers`（缺失结果、无对照孔、阳性对照不显阳性、阴性对照不显阴性等），页面据此提示阻塞原因。
5. `confirm_plate`：整板结果齐全且对照有效后才确认（`testing` → `confirmed`），否则返回 409 并列出全部阻塞原因。确认后：
   - 阳性样本转入`resulted`，写入`result_plate_code`和`result_well`（可回溯到具体孔位/观察）；
   - 阴性样本直接`closed`；
   - 复测样本再次确认时沿用第一次结果（原始结果与板号不变，新读数记入`result_history`），阳性保持`resulted`、阴性保持`closed`。
6. `abandon`：作废排错的板（`draft`/`testing` → `abandoned`），释放样本供重新排样。

根页面是一个实验室操作台：可建板、点孔排样（样本/对照切换）、录结果，橙色虚线框标注缺失结果、红色框标注失效对照，并展示阻塞原因。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/plates/<id>/readiness`：检测板孔位、缺失结果与阻塞原因。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
