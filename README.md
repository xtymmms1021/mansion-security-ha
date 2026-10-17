# Mansion Security 自动应答 · Home Assistant 自定义集成

版本 0.1.0，验证目标 Home Assistant Core **2026.9.4 / Python 3.14**。适用于此次账号绑定的大厅 `ロビーインターホン`，使用 App 已有预设。

手机关机时，独立 HTTPS 客户端和隔离的真实 HA Core 实例均已完成“关闭 → 稍后再来 → 关闭”的写入和回读。**家中 HA OS 尚未安装，楼下实际呼叫及语音应答尚未验收。** 当前模式已恢复关闭。本包作为待现场验收的版本交付。

## GitHub 与 HACS

源码仓库：[xtymmms1021/mansion-security-ha](https://github.com/xtymmms1021/mansion-security-ha)，当前为私有仓库。

本项目已具备 HACS 自定义集成需要的 `custom_components/mansion_security` 目录、`hacs.json`、版本、文档及问题跟踪地址。原版 HACS 从 GitHub 下载文件时没有携带私有仓库访问凭据，因此**不能仅靠添加这个私有仓库地址来安装**。HACS 上游的校验工具也明确拒绝私有仓库。

若另有公开安装仓库，在 HACS 右上角“⋮ → 自定义存储库”粘贴公开仓库地址，类别选“集成 / Integration”，添加后打开 Mansion Security 并下载，再重启 HA、按下文导入会话。请以获准公开后提供的实际安装仓库地址为准。

继续保持全部私有时，使用下方文件复制方式安装，或从私有仓库的 Release 下载完整安装包。为 HACS 增加任意私有仓库权限并不能修复它的无认证文件下载路径，本项目也不会修改 HACS 的内部代码。

依据：[HACS 文件下载实现](https://github.com/hacs/integration/blob/adc26ec798021d2c2db1e6aa90eabe2e208b8c04/custom_components/hacs/base.py#L669-L699)、[私有仓库校验限制](https://github.com/hacs/integration/blob/adc26ec798021d2c2db1e6aa90eabe2e208b8c04/action/action.py#L155-L160)、[自定义仓库说明](https://www.hacs.dev/docs/faq/custom_repositories/)。

## 安装到 HA OS（保持私有）

1. 解压安装包。将包内整个 `custom_components/mansion_security` 文件夹复制到 HA 的 `/config/custom_components/mansion_security`。可使用自己已安装的文件编辑或文件共享工具。如果 `custom_components` 不存在，先创建它。
2. 检查路径应为 `/config/custom_components/mansion_security/manifest.json`，避免重复嵌套目录。无需复制 `tools`、`tests` 到 HA，也无需在 HA OS 中安装 Python 或 ADB。
3. 重启 Home Assistant。
4. 在本次 Windows 电脑上双击 `tools/start_setup.cmd`。在打开的本机页面点击“查看并复制 HA 私密会话码”，复制 `MSHA1.` 开头的完整内容。此次迁移已完成，无需再次输入密码或改密。
5. 在 HA 中打开“设置 → 设备与服务 → 添加集成”，搜索 **Mansion Security**，粘贴私密会话码。集成会先读取服务端设置，成功后创建“自动应答模式”实体。
6. 账号只绑定一个住户房间，接口也只返回该房间，因此目标自动确定。无需输入房号或另选设备。
7. 在实体详情中查看并记下实际实体 ID，供自动化使用。把实体加入仪表盘即可选择预设。首次状态应显示“关闭”。

如果添加集成列表中没有出现它，请检查复制路径和 HA 日志，并确认已经重启。HA 对自定义集成显示通用提示属于正常行为。

## 会话和账号

此服务限制单账号并行登录。你已选择把唯一账号交给 HA，手机 App 应保持退出。HA 导入现有会话后直接读写，启动、重启和普通同步都不会再次调用登录，也不会重新修改密码。

会话码含访问密钥、门铃配对资料和原设备登记令牌，属于私密凭据，Base64 编码不是加密。请把完整会话码保存到自己的密码管理器；安装包不含任何真实账号凭据。HA 在自己的配置存储中保存会话，备份也应按凭据保管。

Windows 工具仅监听 `127.0.0.1`，会话资料使用当前 Windows 用户的 DPAPI 加密保存在 `%LOCALAPPDATA%\MansionSecurityHA`。密码不会写入文件。该目录不在安装包内。当前电脑已经准备好资料；更换电脑时可在工具的输入页粘贴先前保存的会话码恢复资料。

复制完成后可关闭工具。HA 正常运行不需要电脑、ADB、手机或本机页面常驻。不要在正常使用期间运行“重新完成账号登记”。

## 模式与行为

| HA 选项 | 模式 ID | 入口门动作 |
| --- | --- | --- |
| 关闭（动作参数为 `off`） | `0` | 关闭自动应答 |
| ただいま手が離せません。後ほどお越し下さい | `24` | 不自动解锁，稍后再来 |
| 荷物はフロアの宅配ボックスへ入れてください（自动开门） | `33` | 自动解锁 |
| どうぞお入りください（自动开门） | `23` | 自动解锁 |

两种自动开门预设默认隐藏。如确实需要，可在集成的“选项”中打开“允许自动开门预设”。每次设置前还会重新读取服务端 `unLock` 标志，避免预设含义变化后误执行。自动开门预设只做了模拟测试，没有实际启用或现场验收。

启用状态与所选预设由服务端同一个设置接口控制；没有独立启用开关。因此用一个 `select` 实体统一控制，关闭也是一个选项。

App 文案说明预设有效期为 **2 小时**。HA 不会为了维持预设而定时重复设置，实际到期行为仍待长时间现场验证。重复执行设置可能重新计算有效时间，自动化应按实际需要触发。

HA 启动时读取真实状态，默认每 60 秒同步。每次写入后立即 GET 回读，只有确认匹配才更新显示。超时、协议异常或写入无法确认时显示“不可用”，不会乐观宣称设置成功；普通网络故障后下一次成功读取可恢复。

## 自动化示例

将 `examples/actions.yaml` 中的 `select.your_mansion_security_mode` 换成实际实体 ID，然后在“开发者工具 → 动作”的 YAML 编辑器中运行其中一个动作：

```yaml
action: select.select_option
target:
  entity_id: select.your_mansion_security_mode
data:
  option: "ただいま手が離せません。後ほどお越し下さい"
```

关闭使用 `option: "off"`，即使前端显示“关闭”，动作仍使用 `off`。预设名称以当前实体的 `options` 属性为准。完整的“离家时设置”示例在 `examples/automations.yaml`，使用前还须替换实际 `person` 实体。

## 配对续期与重新认证

此次服务端返回的配对期限为 2027/04/17，期限字段为 6 个月。HA 在期限前 7 天开始使用已保存的配对资料续期，保存新的到期日。手机关机时的同一接口调用已验证成功；实际跨越六个月的长期运行尚未验证。

如果服务端拒绝会话，实体会不可用，HA 会显示重新认证入口。处理步骤：

1. 暂时停用 HA 中的 Mansion Security 集成，确认手机 App 已正常退出。
2. 在 Windows 双击 `tools/start_setup.cmd`，进入“会话失效后，重新完成账号登记”。没有本机资料时，先导入以前保存的私密会话码；它也提供重新配对所需资料，即使旧访问密钥已失效。
3. 阅读注意事项，输入用户 ID、当前密码、新密码和昵称，自行提交。该过程遵循 App 的正常流程，**会实际修改密码**。新密码须 8–32 位半角字母、数字或符号，并与当前密码不同。
4. 成功后复制新会话码，在 HA 的重新认证表单中提交，启用集成并检查真实状态。必须为同一账号。

若出现 `alreadyLoggedIn`，服务端仍认为账号被占用，应先处理原 App 的正常退出，不要连续尝试初始化。若密码已经改好、后续登记失败，工具只提供“重试剩余登记”，不会再次改密。

若改密请求超时、结果无法确认，工具会阻止重放。先使用官方 App 确认当前有效密码并正常退出，备份 `%LOCALAPPDATA%\MansionSecurityHA\registration-status.json` 后移走该状态文件，再打开工具处理。保留 `session.dpapi`，不要猜测密码状态反复初始化。

账号迁回手机可能需要完整扫描配对和改密流程，也可能令 HA 旧会话失效；目前没有证据支持两端同时在线登录。

## 现场验收（待完成）

保持安卓手机完全关机，在家中 HA OS 安装并导入后：

1. 确认实体为关闭，然后选择“ただいま手が離せません。後ほどお越し下さい”。等待实体回读确认。
2. 在楼下大厅呼叫本住户，确认听到“稍后再来”对应的应答，且入口门没有解锁。
3. 在 HA 选择关闭，再次呼叫并确认恢复通常行为。
4. 重启 HA，确认能恢复真实状态；不需要重新登录。

此次接口没有暴露服务端或门铃固件的内部执行位置，因此现在能证明的是云端设置在手机离线时可修改和保存。实际离线应答是否生效，需要上述呼叫验收确认。

## 验证与源码

57 项离线测试在真实 HA Core 2026.9.4 / Python 3.14.4 环境通过；另有 1 项真实云端 HA 动作测试通过。后者在隔离测试实例执行，并未安装到家中 HA OS。测试范围和未测项目见 `docs/协议验证记录.txt` 与 `docs/verification.json`。

开发测试环境：Linux、Python 3.14，安装 `requirements-test.txt` 后在包根目录执行 `python -m pytest -q`。默认测试禁止外网访问，网络测试只使用本机模拟服务，不需要真实凭据。协议客户端位于 `api.py`，与 HA 配置、协调器、实体分离。

卸载：先在 HA 中选择关闭并确认，随后删除集成条目，再移除 `/config/custom_components/mansion_security` 并重启。删除集成不会自动注销云端账号或恢复手机登录。

参考：[HA Select 实体规范](https://developers.home-assistant.io/docs/core/entity/select/)、[HA 配置流程规范](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)、[官方应用介绍](https://play.google.com/store/apps/details?id=jp.co.secom.MansionSecurityApp)。
