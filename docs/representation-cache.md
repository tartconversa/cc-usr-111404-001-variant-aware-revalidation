# 表示变体缓存与条件重验契约

在[来源契约](source-contract.md)基础上，app/cache.py与app/collector.py提供可持久恢复的表示缓存、条件重验与交付证据。同一资讯地址的中文摘要、英文摘要、授权合作版各自成变体，互不串用。

## 键模型与身份隔离

缓存记录按(身份范围, URL)组织：身份范围默认是`scope:<credential_scope>`，一条记录容纳该URL的多个表示变体。合作方身份之间默认隔离，互不可见。
仅当来源配置显式给出shared_scope且响应允许共享（带Authorization的请求需响应含public/must-revalidate/s-maxage；不得含private）时，才写入`shared:<shared_scope>`记录。
每个变体保存自己的Vary选择条件（归一化后的指定请求头）、响应头、实体SHA-256与存储时间；凭据只用于请求，不落盘、不进证据。Vary涉及敏感头（Authorization/Cookie等）或为`*`的响应不存储。

## 变体选择与新鲜度

查找时对每个变体按其Vary字段逐一比较归一化请求头（去外围空白；Accept系协商字段小写并规整逗号），全部相等才可选中。
新鲜度只依据显式协议信号：Cache-Control max-age优先，否则Expires-Date；Age头与Date参与当前年龄计算，时间由调用者显式传入。无缓存依据不得声称fresh；no-cache响应每次重验；no-store不存储并使匹配旧变体失效。不使用启发式新鲜度。

## 条件重验与304合并

stale变体携带ETag（弱ETag可用于If-None-Match）与Last-Modified发出条件请求。304按RFC 9111 §4.3.4合并：304出现的头字段替换已存值，未出现的保留，存储时间更新为重验时刻。
- 304未携带实体且本地实体缺失：不得凭空生成正文，改发无条件请求恢复；无条件请求仍回304则交付失败。
- 304的Vary与已存不一致（含Vary: *）：旧变体失效，无条件重取。
- 304含no-store：旧变体失效，重取结果不存储。
- 新200的Vary与匹配本请求的旧变体不同：旧变体不再沿用。

## 并发协调

并行领取相同变体合并为一次传输（单飞）：首位领取者为leader，其余为follower共享结果。取消一个等待者不影响其他领取者；leader取消时结果仍留给等待者。每个变体有序号，存储与304合并按序号比较-交换：迟到的旧重验不得覆盖较新正文，被取代时重新选择当前数据交付。

## 重定向

采集层手动跟随跳转（上限可配，默认5）：同源跳转保留请求头，跨站跳转剥离Authorization等敏感头，证据记录每跳与是否携带凭据。跳转响应本身不缓存，最终URL的表示按最终地址缓存。

## 落盘布局与恢复

```
<缓存目录>/
  records/<范围slug>/<url摘要>.json   # 变体记录，临时文件+原子替换
  bodies/<实体sha256>                 # 实体字节，按内容寻址
```

进程重启后新实例直接恢复：fresh命中不再发请求，stale变体继续携带验证器重验。损坏记录与实体隔离为`.bad`并按未命中处理；读取实体时校验SHA-256。孤儿实体不自动回收。

## 交付证据

每次collect返回Delivery，evidence字段可JSON序列化，关键项：

- `cache.decision`：hit / revalidated / fetched / failed；`cache.lookup_reason`与`cache.unconditional_refetch`说明何时只能重新获取（no-record、no-variant-match、entity-missing、stale、no-freshness-basis、no-cache-directive、request-no-cache、vary-changed、no-store、stale-no-validator、304-without-entity等）。
- `body`：sha256、字节数、origin（stored-variant / network）与来源变体id——复用了哪份正文。
- `freshness`：granted_by（stored-response / revalidation-304 / network-response）、basis、lifetime与age秒数、响应Date与ETag——依据哪个响应获得新鲜度。
- `variant`：变体id、Vary与归一化请求头；`cache.record`：落盘记录相对路径。
- `revalidation`：发出的条件头与304合并结果；`redirects`：跳转链；`flight`：单飞角色；`events`：决策轨迹。
- `configured_request_headers`已脱敏，`sensitive_headers_redacted`列出被隐去的头名；凭据值不出现在任何证据与落盘文件中。

## 边界

仅缓存GET；请求侧支持no-cache、max-age=0、no-store、only-if-cached，不支持min-fresh/max-stale；跳转响应不缓存，每次领取会重新请求跳转跳；实体按内容寻址共享于同一缓存目录，访问隔离由记录层保证。
