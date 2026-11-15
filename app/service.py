"""将来源字节身份与当前响应概览一起交给调用者；另提供表示变体领取入口。"""
import json
import sys
import time
from pathlib import Path
from .source import from_packet,fetch
from .baseline import analyze
from .collector import Collector

def inspect_response(response):
    return {'source_id':response.source_id,'response_sha256':response.sha256,'summary':analyze(response)}

def inspect(packet):return inspect_response(from_packet(packet))

def collect_one(url,source_id,**kwargs):return inspect_response(fetch(url,source_id,**kwargs))

def collect(config,cache_root,*,now,collector=None,cancel_event=None):
    """按来源配置领取一个表示变体；并发领取应共享同一collector以合并传输。"""
    if collector is None:collector=Collector(cache_root)
    return collector.collect(config,now=now,cancel_event=cancel_event)

def main():
    if len(sys.argv)>=2 and sys.argv[1]=='collect':
        if len(sys.argv)!=4:raise SystemExit('用法: python -m app.service collect <来源配置JSON> <缓存目录>')
        config=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
        delivery=collect(config,sys.argv[3],now=time.time())
        print(json.dumps(delivery.evidence,ensure_ascii=False,indent=2))
        if not delivery.ok:raise SystemExit(1)
        return
    if len(sys.argv)!=2:raise SystemExit('请指定来源响应JSON路径')
    print(json.dumps(inspect(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
