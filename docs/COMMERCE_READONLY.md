# 电商只读接入样例

2026-10-17。本样例专注一次授权商品查询，使用合成商品和确定性模型，不需要密钥或模型服务。

```bash
uv run --no-sync python examples/commerce_readonly_demo.py
```

核心装配只有现有的业务存储、只读应用和 Harness：

```python
from forgeharness.applications.commerce import commerce_readonly_application
from forgeharness.sdk import Harness

app = commerce_readonly_application(
    store=store,
    model_factory=model_factory,
    authorize=current_host_access,
)
harness = Harness(app, state_dir=private_state_dir)
result = await harness.run(context=authenticated_context, task=question)
```

变量由宿主提供。完整可执行例子见 `examples/commerce_readonly_demo.py`，其中还显式构造
RunContext 的 actor、tenant、资源范围和稳定 run ID。示例模型读取结构化 `{"sku":"sample"}`，
不是自然语言理解效果的演示；替换 model_factory 后仍须使用同一工具/权限/验收链路。

## 保留的约束

- 只注册 `product_lookup`。搜索、工单、支付、改价、shell 等均未开放；伪造写入调用无法获得执行或审批。
- 查询必须满足 `customer` 角色及 `product:SKU` 范围，并从当前 tenant 的台账读取。
- `authorize(context)` 是宿主查询当前权限的函数，只有返回 `True` 才允许访问。
  初次运行、inspect/恢复等控制操作、模型准备和实际分派前都重查。
- 查询和最终验收还会检查当前授权；撤权发生在工具返回后时，下一次模型调用拒绝读取该结果。
- 最终输出必须等于已完成且仍匹配当前版本的台账回执。模型写“已完成”不能代替验收。
- 控制器重建后可以读取持久结果，但仍需当前授权；失败、完成和删除的运行 ID 不可当作新任务复用。

宿主负责真实身份认证和当前资源清单。示例的内存用户集合只是接线夹具，不能替代登录系统。
新应用通过 `ApplicationSpec.authorize_run` 选择性启用运行级当前授权；其他既有应用默认仍由
其宿主/会话层负责认证，不能声称本次自动为所有应用补齐了 IAM。
这个回调也不能替代工具内部的业务授权、来源撤回验证或原子事务。

## 业务边界

使用的是本地模拟 CommerceStore，不是真实店铺 API。价格单位为 cents，货币与实时接口
由未来业务适配器负责；本入口不提供推荐、澄清、FAQ、订单和通用人工转接。
返回当前查询时刻的版本化结果；先前已发送给模型或用户的内容不能因撤权而追回。
若需要多轮历史和资料撤回，继续使用 SessionService 与宿主来源验证器，不能拿这个单次示例替代。

当前框架结果可重建 controller 后 inspect；将它开放为 HTTP 服务仍需要认证、CSRF、保留/删除、
网络与并发方面的宿主实现和验收。设计见 [ADR 021](adr/021-readonly-commerce-host-access.md)。
