# 产业资讯采集的表示变体缓存与条件重验

现有组件读取一份来源响应，输出当前概览及字节摘要。尚不具备持续领取或跨响应业务归并能力。
app/source.py保留响应状态、重复头和实体原字节，一次请求不自动跟随重定向；app/baseline.py解释当前单响应，app/service.py将概览绑定到来源身份及摘要。
app/cache.py按(身份范围, URL)持久化表示变体，处理Vary、ETag、缓存年龄与304合并；app/collector.py按来源配置领取变体，协调条件重验、跳转授权剥离与并行单飞，每次交付给出可追溯证据。
app/lab.py提供回环HTTP来源，支持GET、POST、HEAD、PROPFIND和REPORT，记录收到的请求，用于在可控来源上联调。fixtures为合成资料，不连接真实企业或个人。
来源协议、字段身份及当前边界见[来源契约](docs/source-contract.md)；缓存键模型、失效规则、并发协调、落盘布局与证据字段见[表示变体缓存契约](docs/representation-cache.md)。Python 3.14，现有代码仅使用标准库。

读取当前响应概览：

    python -m app.service fixtures/sample.json

按来源配置领取一个表示变体并打印交付证据（缓存落盘于给定目录，可跨进程恢复）：

    python -m app.service collect fixtures/collect-config.json .cache-demo

运行现有模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests

如需单独启动本地来源，运行下列命令后读取打印的回环地址；结束时按Ctrl+C：

    python -m app.lab fixtures/sample.json
