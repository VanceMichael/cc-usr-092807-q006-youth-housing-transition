# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

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

## 住房保障资格迁移

`src/civicflow/housing.py` 与 `housing_policy.py` 承载同一家庭在筑梦驿站、
保障性租赁住房、公共租赁住房和安居房之间的连续保障轨迹：

- **按生效时间保存**：家庭关系（含需照顾成员）、房源项目政策版本、房间、
  证明材料、申请、资格认定、轮候冻结名次、租约、缴费、租金减免和交接退出。
- **资格变化只影响后续安排**：新认定把旧资格在新生效日截止（`superseded`），
  已履行租期与缴费事实永不重写；政策换版后历史认定保留当时的 `policy_digest`
  与事实快照。
- **不重复占用房源**：一间房、一个家庭同时只能有一份未结合约（数据库部分唯一索引
  保证）；换房与退租必须先完成交接清单并结清费用，房间才释放；跨保障类型
  不能直接换房，需重新申请认定。
- **证明去重与冲突暂停**：同一 `dedup_key` 重复提交幂等返回，不产生第二次
  资格；同键内容冲突挂起裁定并暂停该家庭在途申请，裁定后自动恢复。
- **轮候排序冻结**：冻结时按累计保障天数（随家庭迁移）与需照顾成员加分确定
  名次，递补严格按冻结名次；放弃递补撤销预备租约并释放房间。
- **减免是例外**：租金减免按例外录入，审批人不得是录入经办人；批准后只作用
  于生效账期及以后，历史账期不变。
- **申请人数据隔离**：经办人持机构通配 scope；申请人仅持 `family:<id>`
  scope，只能查看本家庭资料。
- **断点恢复**：`ensure_recovery_jobs` 幂等补齐即将到期租约、缺件申请催办和
  房源释放任务；`run_recovery` 认领处理，欠费或交接未完成时任务回到待认领。
- **可解释**：`explain_support` 汇总各阶段获得、转换、到期失去的依据、
  租约、实缴金额、轮候名次和完整事件时间线，供审核人员向家庭解释。

运行住房保障端到端演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/housing-demo.sqlite3 housing-demo
```
