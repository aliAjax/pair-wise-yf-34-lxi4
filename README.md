# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

## 抢占结算

飞行计划、空域限制、应急授权和通知通过占用台账 `occupancies` 接成统一结算：

- **一份有效占用**：同一航路（航线包围盒加 0.002° 缓冲）同一时段只允许一份 `active` 占用；普通批准产生 `normal` 占用，指挥官的应急救援授权产生 `rescue` 占用。
- **救援抢占**：`POST /api/plans/{id}/rescue`（或指挥官带 `override_reason` 的 approve）只挤掉**版本未变**的普通计划；被挤掉的占用记为 `preempted`，计划进入 `awaiting_reschedule`（待重排），占用上保留原批准依据（审批人、offline_id、理由、版本）和“被谁挤掉”的救援授权信息，运营方在计划详情和 `GET /api/occupancies` 都能看到。
- **补位**：限制解除（`POST /api/restrictions/{id}/release`）或授权撤销（`POST /api/rescue/{id}/revoke`）后，待重排计划按**起飞时间、再按版本**重新补位，沿用原审批依据（占用 `cycle` 递增）；计划已改版或当前仍有限制/占用冲突的不补位。
- **依据一变立即失效**：限制新设会让**还没生效**的普通占用立即 `invalidated`（已起飞的继续完成，救援不受常规限制影响）；计划一改版，其未结束占用立即失效，需重新提交审核。
- **并发抢占**：两名指挥官同时抢占同一航路时段时，`BEGIN IMMEDIATE` 保证只有先结算的一方生效，后到者收到 `409 rescue_conflict`，错误详情 `current_occupancy` 给出当前占用方（呼号、指挥官、任务），且不留任何半成品。
- **部分失败与重启**：结算先落库（占用、授权、计划状态同事务），通知和审计进 `outbox` 发件箱，按 `event_key` 幂等投递；写入失败时已完成部分保留，只重试没写入的单条事件。进程重启会执行启动对账（修复“撤销已落库但补位未跑”等半成品状态、收敛重复 active 占用），不会重复占位或重复通知。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区（自动失效尚未生效的普通占用）。
- `POST /api/restrictions/{id}/release`：解除限制并触发补位。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束、限制和当前有效占用冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/rescue`：指挥官应急救援授权，抢占同航路时段的普通占用。
- `POST /api/rescue/{id}/revoke`：撤销救援授权，救援机退回待审并触发补位。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消；变更立即失效未结束占用并触发补位。
- `GET /api/occupancies`：占用台账（含占用依据、抢占来源）与待重排队列。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期也触发补位）。
- `POST /api/reconcile`、`GET /api/state`：手工对账与按角色查看总览。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：常规审批与离线幂等、限制冲突与紧急覆盖、救援抢占与依据保留、按起飞时间/版本补位、限制解除重开占用、改版立即失效、两名指挥官并发只一方生效、发件箱部分失败只重试未写入项、进程重启不重复占位/通知。

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制；救援与救援之间不能互相抢占（先到先得）。身份头、无签名离线审核以及单机 SQLite（线程级连接 + WAL）适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
