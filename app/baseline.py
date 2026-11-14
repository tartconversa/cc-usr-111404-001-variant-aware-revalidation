"""当前单响应概览。"""
def analyze(response):
    return {'status':response.status,'bytes':len(response.body),'etag':response.header('etag'),
            'vary':response.header('vary'),'content_type':response.header('content-type')}
