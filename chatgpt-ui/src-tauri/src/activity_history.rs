//! Bounded disk-backed activity index and paging. Never mutates Runtime logs.
use std::{collections::{HashMap,HashSet,VecDeque},fs::{self,File},io::{BufRead,BufReader,Seek,SeekFrom},path::{Path,PathBuf},time::UNIX_EPOCH};
const ITEM_LIMIT:usize=10_000;
const PAGE_LIMIT:usize=256;

fn files(root:&Path)->Result<Vec<PathBuf>,String>{
    if !root.exists(){return Ok(vec![])}
    let mut paths=fs::read_dir(root).map_err(|e|e.to_string())?.flatten().map(|x|x.path())
        .filter(|p|p.is_file()&&p.file_name().and_then(|n|n.to_str())
            .is_some_and(|n|n.starts_with("events")&&n.ends_with(".jsonl"))).collect::<Vec<_>>();
    paths.sort_by_key(|p|p.metadata().and_then(|m|m.modified()).unwrap_or(UNIX_EPOCH));
    Ok(paths)
}
fn scan(paths:&[PathBuf],mut visit:impl FnMut(usize,u64,&str))->Result<(),String>{
    for (i,p) in paths.iter().enumerate(){
        let mut f=BufReader::new(File::open(p).map_err(|e|e.to_string())?);
        let(mut offset,mut line)=(0_u64,String::new());
        loop{
            line.clear();
            let n=f.read_line(&mut line).map_err(|e|e.to_string())?;
            if n==0{break}
            if line.ends_with('\n'){visit(i,offset,&line)}
            offset+=n as u64;
        }
    }
    Ok(())
}
#[derive(Default)]
struct Index{ id:String,started:Option<String>,completed:Option<String>,last:String,files:HashSet<String>,tools:usize,additions:usize,deletions:usize }
fn bootstrap(root:&Path,controllers:&Path)->Result<(u64,String),String>{
    let offset=root.join("events.jsonl").metadata().map(|m|m.len()).unwrap_or(0);
    let mut records=Vec::<Index>::new();
    let mut lookup=HashMap::<String,usize>::new();
    scan(&files(root)?,|_,_,line|{
        if !line.contains("\"taskId\""){return}
        let Ok(v)=serde_json::from_str::<serde_json::Value>(line) else{return};
        let Some(id)=v.get("taskId").and_then(|x|x.as_str()) else{return};
        if !crate::valid_task_id(id){return}
        if v.get("controllerId").and_then(|x|x.as_str()).is_some_and(|x|x.starts_with("system:"))
          ||v.get("threadId").and_then(|x|x.as_str()).is_some_and(|x|x.starts_with("system-")){return}
        let i=*lookup.entry(id.to_string()).or_insert_with(||{
            let n=records.len();records.push(Index{id:id.into(),..Index::default()});n
        });
        let r=&mut records[i];
        if let Some(t)=v.get("timestamp").and_then(|x|x.as_str()){
            if t>r.last.as_str(){r.last=t.into()}
        }
        match v.get("type").and_then(|x|x.as_str()){
            Some("task.started") if r.started.is_none()=>r.started=Some(line.into()),
            Some("task.completed")=>r.completed=Some(line.into()),
            Some("tool.completed"|"tool.failed")=>r.tools+=1,
            Some("file.changed")=>{
                if let Some(path)=v.get("path").or_else(||v.get("sourcePath")).and_then(|x|x.as_str()){
                    if r.files.len()<1000 { r.files.insert(path.to_string()); }
                }
                if let Some(diff)=v.get("diff").and_then(|x|x.as_str()){
                    for part in diff.lines(){
                        if part.starts_with("+++")||part.starts_with("---"){continue}
                        if part.starts_with('+'){r.additions+=1}
                        if part.starts_with('-'){r.deletions+=1}
                    }
                }
            },
            _=>()
        }
    })?;
    let starts=records.iter().filter_map(|r|r.started.as_deref()).collect::<String>();
    let mut output=crate::archived_task_starts(&starts,controllers);
    output.push_str(&starts);
    for r in &records {
        if !r.last.is_empty(){
            output.push_str(&serde_json::json!({"type":"task.indexed","taskId":r.id,"timestamp":r.last}).to_string());
            output.push('\n');
        }
    }
    for r in &records{if let Some(done)=&r.completed{output.push_str(done)}}
    for r in &records {
        if !r.last.is_empty(){
            output.push_str(&serde_json::json!({
                "type":"task.summary_indexed","taskId":r.id,"timestamp":r.last,
                "filesChanged":r.files.len(),"toolCalls":r.tools,
                "additions":r.additions,"deletions":r.deletions
            }).to_string());
            output.push('\n');
        }
    }
    Ok((offset,output))
}
#[derive(Clone,Copy)]
struct Loc{file:usize,offset:u64}
struct Item{id:String,start:Loc,finish:Option<Loc>,at_ms:u64}
// The Runtime history uses UTC ISO timestamps; the inbox uses Unix millis.
// Keep both on one integer timeline without a heavyweight time dependency.
fn millis(v:&serde_json::Value)->u64{
    if let Some(ms)=v.get("timestampMs").and_then(|x|x.as_u64()){return ms}
    let Some(t)=v.get("timestamp").and_then(|x|x.as_str()) else{return 0};
    let read=|start:usize,end:usize|->Option<i64>{t.get(start..end)?.parse::<i64>().ok()};
    let (Some(mut y),Some(m),Some(d),Some(h),Some(min),Some(s))=
        (read(0,4),read(5,7),read(8,10),read(11,13),read(14,16),read(17,19)) else{return 0};
    if !(1..=12).contains(&m)||!(1..=31).contains(&d)||h>23||min>59||s>60{return 0}
    y-=i64::from(m<=2);
    let era=if y>=0{y}else{y-399}/400;
    let yoe=y-era*400;
    let mp=m+if m>2{-3}else{9};
    let doy=(153*mp+2)/5+d-1;
    let days=era*146097+(yoe*365+yoe/4-yoe/100+doy)-719468;
    if days<0{return 0}
    let fractional=t.get(19..).unwrap_or("").strip_prefix('.')
        .map(|s|s.chars().take_while(|c|c.is_ascii_digit()).take(3).collect::<String>());
    let ms=fractional.map(|s|format!("{s:0<3}").parse::<u64>().unwrap_or(0)).unwrap_or(0);
    ((days*86_400+h*3_600+min*60+s)as u64)*1000+ms
}
fn insert(items:&mut VecDeque<Item>,open:&mut HashMap<String,usize>,seq:&mut usize,item:Item,pending:bool){
    let ordinal=*seq;*seq+=1;
    if pending{open.insert(item.id.clone(),ordinal);}
    items.push_back(item);
    if items.len()>ITEM_LIMIT{
        if let Some(old)=items.pop_front(){
            if open.get(&old.id).copied()==Some(ordinal-ITEM_LIMIT){open.remove(&old.id);}
        }
    }
}
fn read_row(paths:&[PathBuf],cache:&mut HashMap<usize,BufReader<File>>,loc:Loc)->Result<String,String>{
    if !cache.contains_key(&loc.file){
        cache.insert(loc.file,BufReader::new(File::open(&paths[loc.file]).map_err(|e|e.to_string())?));
    }
    let f=cache.get_mut(&loc.file).ok_or("历史读取器不可用")?;
    f.seek(SeekFrom::Start(loc.offset)).map_err(|e|e.to_string())?;
    let mut row=String::new();f.read_line(&mut row).map_err(|e|e.to_string())?;
    if !row.ends_with('\n'){return Err("历史日志已轮换，请重试".into())}
    Ok(row)
}
fn page(root:&Path,id:&str,before:Option<usize>,limit:Option<usize>)->Result<(usize,usize,String),String>{
    if !crate::valid_task_id(id){return Err("无效的租约 ID".into())}
    let mut paths=files(root)?;
    let(mut items,mut pending,mut seq)=(VecDeque::new(),HashMap::<String,usize>::new(),0_usize);
    scan(&paths,|file,offset,line|{
        if !line.contains(id){return}
        let Ok(v)=serde_json::from_str::<serde_json::Value>(line) else{return};
        if v.get("taskId").and_then(|x|x.as_str())!=Some(id){return}
        let kind=v.get("type").and_then(|x|x.as_str()).unwrap_or("");
        let at=Loc{file,offset};
        if kind=="tool.started"{
            let key=v.get("callId").or_else(||v.get("id")).and_then(|x|x.as_str())
                .map(str::to_owned).unwrap_or_else(||format!("{file}:{offset}"));
            insert(&mut items,&mut pending,&mut seq,Item{id:key,start:at,finish:None,at_ms:millis(&v)},true);
        }else if kind=="tool.completed"||kind=="tool.failed"{
            let key=v.get("callId").or_else(||v.get("id")).and_then(|x|x.as_str())
                .map(str::to_owned).unwrap_or_else(||format!("{file}:{offset}"));
            let base=seq.saturating_sub(items.len());
            if let Some(n)=pending.remove(&key){
                if n>=base{if let Some(item)=items.get_mut(n-base){item.finish=Some(at);return}}
            }
            insert(&mut items,&mut pending,&mut seq,Item{id:key,start:at,finish:None,at_ms:millis(&v)},false);
        }else if matches!(kind,"agent.progress"|"ui.message"|"agent.observation"|"file.changed"){
            if kind=="agent.progress"&&v.get("text").and_then(|x|x.as_str())==Some("Sleeping..."){return}
            insert(&mut items,&mut pending,&mut seq,
                Item{id:format!("{file}:{offset}"),start:at,finish:None,at_ms:millis(&v)},false);
        }
    })?;
    // User messages live in the bounded UI inbox, not in events*.jsonl.
    // Include their offsets in the same chronological 10k-item browse window.
    let mut ordered:Vec<Item>=items.into_iter().collect();
    let inbox=root.join("ui-inbox.jsonl");
    if inbox.exists(){
        let index=paths.len();
        paths.push(inbox.clone());
        scan(&[inbox],|_,offset,line|{
            if !line.contains(id){return}
            let Ok(v)=serde_json::from_str::<serde_json::Value>(line) else{return};
            if v.get("taskId").and_then(|x|x.as_str())!=Some(id)
                ||v.get("type").and_then(|x|x.as_str())!=Some("ui.message"){return}
            ordered.push(Item{
                id:format!("inbox:{offset}"), start:Loc{file:index,offset},
                finish:None,at_ms:millis(&v),
            });
        })?;
    }
    ordered.sort_by_key(|item|(item.at_ms,item.start.file,item.start.offset));
    if ordered.len()>ITEM_LIMIT{ordered.drain(..ordered.len()-ITEM_LIMIT);}
    let total=ordered.len();
    let end=before.unwrap_or(total).min(total);
    let start=end.saturating_sub(limit.unwrap_or(200).clamp(1,PAGE_LIMIT));
    let mut cache=HashMap::new();
    let mut output=String::new();
    for entry in ordered.iter().skip(start).take(end-start){
        output.push_str(&read_row(&paths,&mut cache,entry.start)?);
        if let Some(at)=entry.finish{output.push_str(&read_row(&paths,&mut cache,at)?)}
    }
    Ok((total,start,output))
}
#[tauri::command]
pub(crate) async fn activity_bootstrap_compact()->Result<(u64,String),String>{
    tauri::async_runtime::spawn_blocking(||{
        bootstrap(Path::new(crate::ACTIVITY_DATA_DIR),Path::new(crate::CONTROLLER_ARCHIVE_DIR))
    }).await.map_err(|e|format!("History indexing task failed: {e}"))?
}
#[tauri::command]
pub(crate) async fn activity_task_page(task_id:String,before:Option<usize>,limit:Option<usize>)->Result<(usize,usize,String),String>{
    tauri::async_runtime::spawn_blocking(move ||{
        page(Path::new(crate::ACTIVITY_DATA_DIR),&task_id,before,limit)
    }).await.map_err(|e|format!("History paging task failed: {e}"))?
}
#[cfg(test)]
mod tests{
    use super::*;
    use std::io::Write;
    #[test]
    fn ten_thousand_items_bounded_and_paged(){
        let root=std::env::temp_dir().join(format!("console-page-{}-{}",std::process::id(),crate::unix_millis()));
        fs::create_dir_all(&root).unwrap();
        let id="11111111111111111111111111111111";
        let mut f=File::create(root.join("events.jsonl")).unwrap();
        writeln!(f,"{}",serde_json::json!({"type":"task.started","taskId":id,"timestamp":"2026-10-14T00:00:00Z"})).unwrap();
        for i in 0..10_050{
            writeln!(f,"{}",serde_json::json!({"type":"agent.progress","taskId":id,"id":format!("event-{i}"),"text":format!("item-{i}"),"timestamp":"2026-10-14T01:00:00Z"})).unwrap();
        }
        drop(f);
        let (total,start,rows)=page(&root,id,None,Some(200)).unwrap();
        assert_eq!((total,start),(10000,9800));
        assert!(rows.contains("item-10049"));
        let (_,start,old)=page(&root,id,Some(200),Some(200)).unwrap();
        assert_eq!(start,0);assert!(old.contains("item-50"));
        let (_,index)=bootstrap(&root,&root.join("no-controllers")).unwrap();
        assert!(index.contains("task.indexed"));assert!(!index.contains("item-10049"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn tool_start_completion_are_one_item(){
        let root=std::env::temp_dir().join(format!("console-tool-{}-{}",std::process::id(),crate::unix_millis()));
        fs::create_dir_all(&root).unwrap();
        let id="22222222222222222222222222222222";
        let mut f=File::create(root.join("events.jsonl")).unwrap();
        for kind in ["tool.started","tool.completed","file.changed"]{
            writeln!(f,"{}",serde_json::json!({"type":kind,"taskId":id,"callId":"a","path":"C:\\a","tool":"run_python"})).unwrap();
        }
        drop(f);
        let (total,_,rows)=page(&root,id,None,None).unwrap();
        assert_eq!(total,2);
        for kind in ["tool.started","tool.completed","file.changed"]{assert!(rows.contains(kind))}
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn inbox_messages_are_paginated_in_event_time_order() {
        let root=std::env::temp_dir().join(format!("console-inbox-{}-{}",std::process::id(),crate::unix_millis()));
        fs::create_dir_all(&root).unwrap();
        let id="33333333333333333333333333333333";
        let mut history=File::create(root.join("events.jsonl")).unwrap();
        writeln!(history,"{}",serde_json::json!({"type":"agent.progress","taskId":id,"id":"first","text":"first","timestamp":"2026-10-14T01:00:00Z"})).unwrap();
        writeln!(history,"{}",serde_json::json!({"type":"agent.progress","taskId":id,"id":"last","text":"last","timestamp":"2026-10-14T01:02:00Z"})).unwrap();
        drop(history);
        let middle=millis(&serde_json::json!({"timestamp":"2026-10-14T01:01:00Z"}));
        assert_eq!(middle-millis(&serde_json::json!({"timestamp":"2026-10-14T01:00:00Z"})),60_000);
        let mut inbox=File::create(root.join("ui-inbox.jsonl")).unwrap();
        writeln!(inbox,"{}",serde_json::json!({"type":"ui.message","taskId":id,"id":"user","timestampMs":middle,"text":"middle"})).unwrap();
        drop(inbox);
        let (total,start,rows)=page(&root,id,None,Some(3)).unwrap();
        assert_eq!((total,start),(3,0));
        let first=rows.find("first").unwrap();
        let middle=rows.find("middle").unwrap();
        let last=rows.find("last").unwrap();
        assert!(first<middle&&middle<last);
        fs::remove_dir_all(root).unwrap();
    }

}