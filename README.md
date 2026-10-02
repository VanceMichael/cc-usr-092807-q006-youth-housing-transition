# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 住房保障资格迁移

`housing` 子域承载一名青年从短期驿站、保障性租赁住房、公租房到安居房的**资格迁移**，
解决旧台账把每次申请当成互不相关新记录、重复占用房源、丢失已缴租金与轮候时间的问题：

- 房源项目/房间、租约、社保就业证明、收入与住房困难认定、家庭关系、轮候、租金减免与退出交接
  全部按**生效时间**仅追加保存；资格变化只影响后续安排，**已履行租期与缴费事实不被重写**。
- 四类保障采用各自的版本化准入条件（`housing_policies.py`）；认定时快照政策版本与判定要素。
- 同一证明重复提交幂等返回，不产生第二次资格；同周期内容冲突登记冲突并**暂停相关申请**，核查后恢复。
- 换房、退租必须**先完成交接**才释放房间、激活下一租约；房源释放后按**冻结的轮候顺位**自动递补，
  offer 过期保留原顺位。
- 经办人**不能审批自己录入**的资格决定或租金减免；申请人凭家庭作用域**只能查看本家庭资料**。
- 租约到期、缺件、offer 过期、房源释放登记为可恢复任务，系统恢复后继续处理
  （交接未完成时不释放房源，任务自动重试）。
- `family_timeline` 与 `explain_support` 向审核人员解释一个家庭为何在各阶段获得、失去或转换支持，
  以及跨阶段累计实缴租金与携带的轮候时间。

运行住房保障端到端演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/housing-demo.sqlite3 --now 2026-10-02T09:00:00+08:00 housing-demo
```


## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```
