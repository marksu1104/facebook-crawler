const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const source=fs.readFileSync(path.resolve(__dirname,'../app/post_metadata.js'),'utf8');
const group='https://www.facebook.com/groups/123456';
const record={id:'story:111',post_id:'111',target_group:{id:'123456'},message:{text:'一段足以識別的測試貼文內容'},actors:[{name:'作者'}],permalink_url:group+'/posts/111/?tracking=remove'};

function scan(scripts,expected=group,functionName='scanPostMetadata',responsePayloads=[]) {
  const dom=new JSDOM('<!doctype html><html><head></head><body></body></html>',{url:group,runScripts:'outside-only'});
  try {
    dom.window.fetch=()=>{throw new Error('Metadata parsing must not use the network');};
    for(const item of scripts){
      const script=dom.window.document.createElement('script');
      script.type=item.type || 'application/json';
      script.textContent=typeof item.content==='string' ? item.content : JSON.stringify(item.content);
      dom.window.document.head.append(script);
    }
    return JSON.parse(JSON.stringify(dom.window.Function('expected','payloads',source+'\nreturn '+functionName+'(expected,payloads);')(expected,responsePayloads)));
  } finally {dom.window.close();}
}

const loaded=(scripts,expected=group,responses=[])=>scan(scripts,expected,'extractLoadedPostMetadata',responses);
const feedResponse=records=>JSON.stringify({data:{node:{__typename:'Group',id:'123456',group_feed:{edges:records.map(node=>({node}))}}}});

test('scanner finds strict nested same-group records without executing script content',()=>{
  const result=scan([{content:{wrapper:[{payload:record}]}},{type:'text/javascript',content:'window.fetch("https://example.test")'}]);
  assert.equal(result.limit_exceeded,false);
  assert.deepEqual(result.records,[{story_id:'story:111',post_id:'111',group_id:'123456',text:record.message.text,actors:['作者'],url:group+'/posts/111/'}]);
});

test('malformed JSON scripts are skipped without discarding valid scripts',()=>{
  const result=scan([{content:'not JSON'},{content:record},{content:'{"broken":'}]);
  assert.equal(result.records.length,1);
  assert.equal(result.limit_exceeded,false);
});

test('foreign target groups, foreign URLs and invalid IDs are excluded',()=>{
  const variants=[
    {...record,target_group:{id:'999999'}},
    {...record,permalink_url:'https://www.facebook.com/groups/999999/posts/111/'},
    {...record,permalink_url:'https://evil.test/groups/123456/posts/111/'},
    {...record,post_id:111},
    {...record,post_id:'pfbidNotANumericId'},
    {...record,permalink_url:group+'/posts/222/'},
    {...record,permalink_url:'http://www.facebook.com/groups/123456/posts/111/'},
    {...record,permalink_url:'https://user@www.facebook.com/groups/123456/posts/111/'},
  ];
  assert.deepEqual(scan([{content:variants}]).records,[]);
});

test('comment-targeting URLs are rejected while an explicit valid alternate URL is accepted',()=>{
  const variants=[
    {...record,permalink_url:group+'/posts/111/?comment_id=7'},
    {...record,permalink_url:group+'/posts/111/?reply_comment_id=8'},
    {...record,permalink_url:group+'/posts/111/?COMMENT_ID=7'},
    {...record,permalink_url:undefined,wwwURL:group+'/permalink/111/'}
  ];
  const result=scan([{content:variants}]);
  assert.equal(result.records.length,1);
  assert.equal(result.records[0].url,group+'/posts/111/');
});

test('records need message text and at least one named actor',()=>{
  const variants=[{...record,message:{text:''}},{...record,message:{text:123}},{...record,actors:[]},{...record,actors:[{name:' '}]}];
  assert.deepEqual(scan([{content:variants}]).records,[]);
});

test('invalid expected-group URLs yield no metadata',()=>{
  for(const expected of ['https://evil.test/groups/123456','http://www.facebook.com/groups/123456','https://www.facebook.com:8443/groups/123456','https://www.facebook.com/share/g/abcdef/']){
    assert.deepEqual(scan([{content:record}],expected).records,[]);
  }
});

test('the total UTF-8 byte budget rejects the whole scan after an earlier valid record',()=>{
  const result=scan([{content:record},{content:'界'.repeat(Math.floor(20*1024*1024/3)+1)}]);
  assert.equal(result.limit_exceeded,true);
  assert.deepEqual(result.records,[]);
});

test('object count is global across scripts and overflow returns no partial metadata',()=>{
  const many='['+new Array(150000).fill('{}').join(',')+']';
  const result=scan([{content:record},{content:many}]);
  assert.equal(result.limit_exceeded,true);
  assert.deepEqual(result.records,[]);
});

test('depth overflow returns no partial metadata',()=>{
  const result=scan([{content:record},{content:'['.repeat(71)+'0'+']'.repeat(71)}]);
  assert.equal(result.limit_exceeded,true);
  assert.deepEqual(result.records,[]);
});

test('loaded metadata returns a complete post from the same validated source object',()=>{
  const content='預載的完整貼文\n保留換行、網址與原始內容，沒有與畫面上的其他貼文猜測配對。';
  const posts=loaded([{content:{preloadedStory:{...record,message:{text:content},actors:[{name:'第一位作者'},{name:'第二位作者'}]}}}]);
  assert.deepEqual(posts,[{author:'第一位作者、第二位作者',content,posted_at:'',url:group+'/posts/111/',post_id:'111'}]);
});

test('duplicate records collapse by stable numeric post ID',()=>{
  const posts=loaded([{content:[record,{...record}]},{content:{again:record}}]);
  assert.equal(posts.length,1);
  assert.equal(posts[0].post_id,'111');
});

test('conflicting text for one ID drops that ID even if another duplicate later agrees',()=>{
  const other={...record,post_id:'222',permalink_url:group+'/posts/222/',message:{text:'另外一篇有效貼文'}};
  const posts=loaded([{content:[record,{...record,message:{text:'同一個ID卻有不同內容'}},record,other]}]);
  assert.deepEqual(posts.map(post=>post.post_id),['222']);
});

test('posts and permalink aliases for one verified group/post ID collapse',()=>{
  const posts=loaded([{content:[record,{...record,permalink_url:group+'/permalink/111/'},record]}]);
  assert.equal(posts.length,1);
  assert.equal(posts[0].url,group+'/posts/111/');
});

test('conflicting actor identity for one ID is held conservatively',()=>{
  const posts=loaded([{content:[record,{...record,actors:[{name:'另一位作者'}]}]}]);
  assert.deepEqual(posts,[]);
});

test('separate post IDs are never collapsed just because their text and actors match',()=>{
  const posts=loaded([{content:[record,{...record,id:'story:222',post_id:'222',permalink_url:group+'/posts/222/'}]}]);
  assert.deepEqual(posts.map(post=>post.post_id),['111','222']);
});

test('loaded source does not return partial posts after a global parser limit',()=>{
  assert.deepEqual(loaded([{content:record},{content:'['.repeat(71)+'0'+']'.repeat(71)}]),[]);
});

test('explicit non-story, unpublished, pending and deleted records are excluded',()=>{
  const variants=[{...record,__typename:'Comment'}, {...record,is_published:false},
    {...record,is_pending:true}, {...record,is_pending_approval:true},
    {...record,is_deleted:true}, {...record,is_draft:true}];
  assert.deepEqual(loaded([{content:variants}]),[]);
  assert.equal(loaded([{content:{...record,__typename:'Story'}}]).length,1);
});

test('later feed response posts retain their own body and original link',()=>{
  const later={...record,post_id:'222',message:{text:'向下載入的第二篇完整貼文'},permalink_url:group+'/posts/222/'};
  const posts=loaded([{content:record}],group,[feedResponse([later])]);
  assert.deepEqual(posts.map(post=>[post.post_id,post.content,post.url]),[
    ['111',record.message.text,group+'/posts/111/'],
    ['222',later.message.text,group+'/posts/222/']
  ]);
});

test('feed responses accept the JSON guard and newline-delimited streaming patches',()=>{
  const later={...record,post_id:'222',permalink_url:group+'/posts/222/'};
  const patch=JSON.stringify({path:['node','group_feed','edges',1,'node'],data:later});
  const posts=loaded([],group,['for (;;);'+feedResponse([record])+'\n'+patch+'\nnot JSON']);
  assert.deepEqual(posts.map(post=>post.post_id),['111','222']);
});

test('non-feed response stories and attached shared posts are excluded',()=>{
  const attached={...record,post_id:'222',permalink_url:group+'/posts/222/'};
  const post={...record,attached_story:attached};
  const unrelated=JSON.stringify({data:{recommendation:attached}});
  const posts=loaded([],group,[unrelated,feedResponse([post])]);
  assert.deepEqual(posts.map(post=>post.post_id),['111']);
});

test('foreign-group stories inside the feed are excluded',()=>{
  const foreign={...record,target_group:{id:'999999'},permalink_url:'https://www.facebook.com/groups/999999/posts/111/'};
  assert.deepEqual(loaded([],group,[feedResponse([foreign])]),[]);
});

test('a streaming patch for an attached share is not another feed publication',()=>{
  const patch=JSON.stringify({path:['node','group_feed','edges',0,'node','attached_story'],data:record});
  assert.deepEqual(loaded([],group,[patch]),[]);
});
