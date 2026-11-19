$ErrorActionPreference="Stop"
$ProgressPreference="SilentlyContinue"
$OutputEncoding=[System.Text.Encoding]::UTF8
[Console]::InputEncoding=[System.Text.Encoding]::UTF8
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class GPTPCWin32 {
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hWnd, ref POINT point);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool PostMessage(IntPtr hWnd, UInt32 msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr hWnd, UInt32 msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr hWnd, UInt32 msg, IntPtr wParam, string lParam);
}
"@
$WM_SETTEXT=0x000C;$WM_KEYDOWN=0x0100;$WM_KEYUP=0x0101
$WM_MOUSEMOVE=0x0200;$WM_LBUTTONDOWN=0x0201;$WM_LBUTTONUP=0x0202;$WM_RBUTTONDOWN=0x0204;$WM_RBUTTONUP=0x0205
$WM_MBUTTONDOWN=0x0207;$WM_MBUTTONUP=0x0208;$WM_MOUSEWHEEL=0x020A;$WM_MOUSEHWHEEL=0x020E;$EM_SETSEL=0x00B1;$EM_REPLACESEL=0x00C2
$script:Snapshots=@{}

function Out-Json($v){[Console]::Out.WriteLine(($v|ConvertTo-Json -Compress -Depth 12));[Console]::Out.Flush()}
function App-Key([string]$a){$a.Trim().ToLowerInvariant()}
function Snapshot-Key([string]$session,[string]$app){$session+"|"+(App-Key $app)}
function Target-IsForeground($s){[GPTPCWin32]::GetForegroundWindow()-eq$s.hwnd}
function Focus-Mode([string]$mode){$m=$mode.Trim().ToLowerInvariant();if([string]::IsNullOrWhiteSpace($m)){$m="background"};if($m-ne"background"-and$m-ne"foreground"){throw "focus_mode must be background or foreground"};$m}
function Focus-State($s,[string]$mode){
  $m=Focus-Mode $mode;$before=Target-IsForeground $s;$activated=$false
  if($m-eq"foreground" -and -not$before){
    if([GPTPCWin32]::IsIconic($s.hwnd)){[void][GPTPCWin32]::ShowWindow($s.hwnd,9)}
    [void][GPTPCWin32]::SetForegroundWindow($s.hwnd);Start-Sleep -Milliseconds 25
  }
  $after=Target-IsForeground $s
  if($m-eq"foreground" -and -not$after){throw "foregroundActivationFailed: target window did not become foreground"}
  if($m-eq"foreground" -and -not$before -and $after){$activated=$true}
  [ordered]@{focus_mode=$m;target_was_foreground=$before;target_is_foreground=$after;activated_foreground=$activated}
}
function With-Focus($result,$focus){foreach($k in $focus.Keys){$result[$k]=$focus[$k]};$result}
function Resolve-App([string]$q){
  if([string]::IsNullOrWhiteSpace($q)){throw "app is required"};$n=$q.Trim();$pq=$n
  if($pq.EndsWith(".exe",[System.StringComparison]::OrdinalIgnoreCase)){$pq=$pq.Substring(0,$pq.Length-4)}
  $ps=@(Get-Process|Where-Object{$_.MainWindowHandle -ne 0});$pidValue=0
  if([int]::TryParse($n,[ref]$pidValue)){$m=$ps|Where-Object{$_.Id -eq $pidValue}|Select-Object -First 1;if($null-ne$m){return $m}}
  $m=$ps|Where-Object{$_.ProcessName -ieq $pq -or "$($_.ProcessName).exe" -ieq $n -or $_.MainWindowTitle -ieq $n -or $_.MainWindowTitle -ilike "*$n*"}|Select-Object -First 1
  if($null-eq$m){throw ('appNotFound("'+$q+'")')};$m
}
function Main-El($p){if($p.MainWindowHandle -eq 0){throw "No top-level window"};[Windows.Automation.AutomationElement]::FromHandle([IntPtr]$p.MainWindowHandle)}
function Frame($e){try{$r=$e.Current.BoundingRectangle;if($r.IsEmpty -or $r.Width-le 0 -or $r.Height-le 0){return $null};[ordered]@{x=[int][Math]::Round($r.X);y=[int][Math]::Round($r.Y);width=[int][Math]::Round($r.Width);height=[int][Math]::Round($r.Height)}}catch{$null}}
function Str($e,[string]$p){try{$v=$e.Current.$p;if($null-eq$v){""}else{[string]$v}}catch{""}}
function Hwnd($e){try{[IntPtr][int64]$e.Current.NativeWindowHandle}catch{[IntPtr]::Zero}}
function Pattern($e,$p){try{$e.GetCurrentPattern($p)}catch{$null}}
function Patterns($e){
  $n=New-Object System.Collections.Generic.List[string]
  if($null-ne(Pattern $e ([Windows.Automation.InvokePattern]::Pattern))){$n.Add("invoke")}
  if($null-ne(Pattern $e ([Windows.Automation.TogglePattern]::Pattern))){$n.Add("toggle")}
  if($null-ne(Pattern $e ([Windows.Automation.SelectionItemPattern]::Pattern))){$n.Add("select")}
  if($null-ne(Pattern $e ([Windows.Automation.ExpandCollapsePattern]::Pattern))){$n.Add("expand_collapse")}
  if($null-ne(Pattern $e ([Windows.Automation.ValuePattern]::Pattern))){$n.Add("value")}
  if($null-ne(Pattern $e ([Windows.Automation.ScrollPattern]::Pattern))){$n.Add("scroll")}
  if($null-ne(Pattern $e ([Windows.Automation.ScrollItemPattern]::Pattern))){$n.Add("scroll_into_view")}
  $n.ToArray()
}
function Role($e){try{$n=[string]$e.Current.ControlType.ProgrammaticName;if($n.StartsWith("ControlType.")){$n.Substring(12)}else{$n}}catch{""}}
function State([string]$app,[string]$session,[int]$maxNodes,[int]$maxDepth){
  if([string]::IsNullOrWhiteSpace($session)){throw "session_id is required"}
  $p=Resolve-App $app;$root=Main-El $p;$w=[Windows.Automation.TreeWalker]::ControlViewWalker;$q=New-Object System.Collections.Queue
  $c=$w.GetFirstChild($root);while($null-ne$c){$q.Enqueue([pscustomobject]@{e=$c;d=1});$c=$w.GetNextSibling($c)}
  $map=@{};$rows=New-Object System.Collections.Generic.List[object];$idx=0;$visited=0
  while($q.Count-gt 0 -and $visited-lt$maxNodes){
    $it=$q.Dequeue();$e=$it.e;$d=[int]$it.d;$visited++;$name=Str $e "Name";$aid=Str $e "AutomationId";$role=Role $e;$f=Frame $e;$pat=@(Patterns $e)
    $meaningful=(-not[string]::IsNullOrWhiteSpace($name) -or -not[string]::IsNullOrWhiteSpace($aid) -or $pat.Count-gt 0 -or @("Button","Edit","ComboBox","ListItem","MenuItem","TreeItem","TabItem","CheckBox","RadioButton","Hyperlink","Slider","Spinner") -contains $role)
    if($meaningful -and $null-ne$f){$idx++;$map[$idx]=$e;$enabled=$true;$off=$false;try{$enabled=[bool]$e.Current.IsEnabled}catch{};try{$off=[bool]$e.Current.IsOffscreen}catch{};$rows.Add([ordered]@{id=$idx;role=$role;name=$name;automation_id=$aid;enabled=$enabled;offscreen=$off;frame=$f;patterns=$pat})}
    if($d-lt$maxDepth){try{$n=$w.GetFirstChild($e);while($null-ne$n){$q.Enqueue([pscustomobject]@{e=$n;d=$d+1});$n=$w.GetNextSibling($n)}}catch{}}
  }
  $snapshotId=[Guid]::NewGuid().ToString("N");$key=Snapshot-Key $session $app
  $script:Snapshots[$key]=@{p=$p;pid=$p.Id;root=$root;hwnd=[IntPtr]$p.MainWindowHandle;elements=$map;snapshotId=$snapshotId}
  [ordered]@{ok=$true;action="state";app=$app;process=$p.ProcessName;pid=$p.Id;title=$p.MainWindowTitle;snapshot_id=$snapshotId;element_count=$rows.Count;scanned_nodes=$visited;truncated=($q.Count-gt 0);elements=$rows.ToArray()}
}
function Snap([string]$session,[string]$app,[string]$snapshotId){
  if([string]::IsNullOrWhiteSpace($session)){throw "session_id is required"};if([string]::IsNullOrWhiteSpace($snapshotId)){throw "snapshot_id is required"}
  $key=Snapshot-Key $session $app;if(-not$script:Snapshots.ContainsKey($key)){throw "No cached UI state for this session/app. Call state first."}
  $s=$script:Snapshots[$key];if([string]$s.snapshotId -ne $snapshotId){throw "staleSnapshot('$snapshotId'). Refresh state and retry."}
  $live=$null;try{$live=Get-Process -Id ([int]$s.pid) -ErrorAction Stop}catch{}
  if($null-eq$live){[void]$script:Snapshots.Remove($key);throw "staleSnapshot('$snapshotId'): target process/window changed. Refresh state and retry."}
  $liveHwnd=[IntPtr]$live.MainWindowHandle
  if($liveHwnd-eq[IntPtr]::Zero -or $liveHwnd-ne$s.hwnd){[void]$script:Snapshots.Remove($key);throw "staleSnapshot('$snapshotId'): target process/window changed. Refresh state and retry."}
  $s
}
function El($s,$id){if($null-eq$id){return $null};$i=[int]$id;if(-not$s.elements.ContainsKey($i)){throw "elementNotFound($i). Refresh state."};$s.elements[$i]}
function Lp([int]$x,[int]$y){[IntPtr]((($y-band 0xffff)-shl 16)-bor($x-band 0xffff))}
function Window-Click([IntPtr]$h,[int]$sx,[int]$sy,[string]$b,[int]$count){
  if($h-eq[IntPtr]::Zero){throw "window handle unavailable"};$pt=New-Object GPTPCWin32+POINT;$pt.X=$sx;$pt.Y=$sy;[void][GPTPCWin32]::ScreenToClient($h,[ref]$pt);$lp=[IntPtr]((($pt.Y-band 0xffff)-shl 16)-bor($pt.X-band 0xffff))
  $down=$WM_LBUTTONDOWN;$up=$WM_LBUTTONUP;$flag=1;if($b-eq"right"){$down=$WM_RBUTTONDOWN;$up=$WM_RBUTTONUP;$flag=2}elseif($b-eq"middle"){$down=$WM_MBUTTONDOWN;$up=$WM_MBUTTONUP;$flag=16}
  for($i=0;$i-lt[Math]::Max(1,[Math]::Min(3,$count));$i++){[void][GPTPCWin32]::PostMessage($h,$WM_MOUSEMOVE,[IntPtr]::Zero,$lp);[void][GPTPCWin32]::PostMessage($h,$down,[IntPtr]$flag,$lp);Start-Sleep -Milliseconds 18;[void][GPTPCWin32]::PostMessage($h,$up,[IntPtr]::Zero,$lp)}
}
function Semantic-Click($s,$e,[string]$b,[int]$count){
  if($b-eq"left" -and $count-eq 1 -and (Target-IsForeground $s)){
    $p=Pattern $e ([Windows.Automation.InvokePattern]::Pattern);if($null-ne$p){$p.Invoke();return "uia_invoke"}
    $p=Pattern $e ([Windows.Automation.TogglePattern]::Pattern);if($null-ne$p){$p.Toggle();return "uia_toggle"}
    $p=Pattern $e ([Windows.Automation.SelectionItemPattern]::Pattern);if($null-ne$p){$p.Select();return "uia_select"}
    $p=Pattern $e ([Windows.Automation.ExpandCollapsePattern]::Pattern);if($null-ne$p){if($p.Current.ExpandCollapseState-eq[Windows.Automation.ExpandCollapseState]::Collapsed){$p.Expand()}else{$p.Collapse()};return "uia_expand_collapse"}
  }
  $f=Frame $e;if($null-eq$f){throw "element has no frame"};$h=Hwnd $e;if($h-eq[IntPtr]::Zero){$h=$s.hwnd};Window-Click -h $h -sx ([int]($f.x+$f.width/2)) -sy ([int]($f.y+$f.height/2)) -b $b -count $count;"window_message"
}
function Text-To($s,$e,[string]$text,[bool]$replace){
  $h=Hwnd $e;if($h-eq$s.hwnd){$h=[IntPtr]::Zero}
  if($h-ne[IntPtr]::Zero){
    if($replace){[void][GPTPCWin32]::SendMessage($h,$WM_SETTEXT,[IntPtr]::Zero,$text);return "hwnd_settext"}
    [void][GPTPCWin32]::SendMessage($h,$EM_SETSEL,[IntPtr](-1),[IntPtr](-1));[void][GPTPCWin32]::SendMessage($h,$EM_REPLACESEL,[IntPtr]1,$text);return "hwnd_edit"
  }
  if($replace -and (Target-IsForeground $s)){$p=Pattern $e ([Windows.Automation.ValuePattern]::Pattern);if($null-ne$p -and -not$p.Current.IsReadOnly){$p.SetValue($text);return "uia_value"}}
  throw "background text path unavailable for this control; type_text will not use UIA ValuePattern fallback because it may steal focus"
}
function Vk([string]$key){$n=$key.Trim().ToLowerInvariant();$m=@{"enter"=13;"return"=13;"tab"=9;"escape"=27;"esc"=27;"backspace"=8;"delete"=46;"space"=32;"left"=37;"up"=38;"right"=39;"down"=40;"home"=36;"end"=35;"page_up"=33;"page_down"=34};if($m.ContainsKey($n)){return $m[$n]};if($n-match"^f([1-9]|1[0-2])$"){return 0x70+[int]$Matches[1]-1};if($n.Length-eq1){$c=[int][char]$n.ToUpperInvariant()[0];if(($c-ge48-and$c-le57)-or($c-ge65-and$c-le90)){return $c}};throw "unsupported key '$key'"}
function Key-To([IntPtr]$h,[string]$key){
  $parts=@($key-split"\+");$main=$parts[$parts.Count-1];$mods=New-Object System.Collections.Generic.List[int]
  for($i=0;$i-lt$parts.Count-1;$i++){switch($parts[$i].ToLowerInvariant()){"ctrl"{$mods.Add(17)}"control"{$mods.Add(17)}"shift"{$mods.Add(16)}"alt"{$mods.Add(18)}"win"{$mods.Add(91)}"super"{$mods.Add(91)}}}
  $v=Vk $main;foreach($m in $mods){[void][GPTPCWin32]::PostMessage($h,$WM_KEYDOWN,[IntPtr]$m,[IntPtr]::Zero)};[void][GPTPCWin32]::PostMessage($h,$WM_KEYDOWN,[IntPtr]$v,[IntPtr]::Zero);Start-Sleep -Milliseconds 12;[void][GPTPCWin32]::PostMessage($h,$WM_KEYUP,[IntPtr]$v,[IntPtr]::Zero);for($i=$mods.Count-1;$i-ge0;$i--){[void][GPTPCWin32]::PostMessage($h,$WM_KEYUP,[IntPtr]$mods[$i],[IntPtr]::Zero)}
}
function One($op,[string]$defaultApp,[string]$session,[string]$snapshotId,[int]$mn,[int]$md){
  $a=([string]$op.action).Trim().ToLowerInvariant();$app=[string]$op.app;if([string]::IsNullOrWhiteSpace($app)){$app=$defaultApp}
  if($a-eq"state"){return State $app $session $mn $md}
  $s=Snap $session $app $snapshotId;$e=El $s $op.element
  if($a-eq"click"){$b=[string]$op.button;if([string]::IsNullOrWhiteSpace($b)){$b="left"};$count=1;if($null-ne$op.count){$count=[int]$op.count};if($null-ne$e){$method=Semantic-Click $s $e $b $count;$best=($method-eq"window_message");return [ordered]@{ok=$true;action=$a;app=$app;element=[int]$op.element;method=$method;best_effort=$best}};if($null-eq$op.x-or$null-eq$op.y){throw "click requires element or x/y"};Window-Click -h $s.hwnd -sx ([int]$op.x) -sy ([int]$op.y) -b $b -count $count;return [ordered]@{ok=$true;action=$a;app=$app;x=[int]$op.x;y=[int]$op.y;method="window_message";best_effort=$true}}
  if($a-eq"type_text"-or$a-eq"set_value"){if($null-eq$e){throw "$a requires element"};$value=[string]$op.text;$method=Text-To $s $e $value ($a-eq"set_value");$best=$method.StartsWith("hwnd_");return [ordered]@{ok=$true;action=$a;app=$app;element=[int]$op.element;method=$method;chars=$value.Length;best_effort=$best}}
  if($a-eq"press_key"){$h=$s.hwnd;if($null-ne$e){$candidate=Hwnd $e;if($candidate-ne[IntPtr]::Zero){$h=$candidate}};Key-To $h ([string]$op.key);return [ordered]@{ok=$true;action=$a;app=$app;element=$op.element;key=[string]$op.key;method="window_message";best_effort=$true}}
  if($a-eq"scroll"){$dir=[string]$op.direction;if([string]::IsNullOrWhiteSpace($dir)){$dir="down"};$amount=1;if($null-ne$op.amount){$amount=[Math]::Max(1,[Math]::Min(10,[int][Math]::Ceiling([double]$op.amount)))}
    if($null-ne$e -and (Target-IsForeground $s)){$p=Pattern $e ([Windows.Automation.ScrollItemPattern]::Pattern);if($null-ne$p){$p.ScrollIntoView();return [ordered]@{ok=$true;action=$a;app=$app;element=[int]$op.element;method="uia_scroll_into_view";best_effort=$false}};$p=Pattern $e ([Windows.Automation.ScrollPattern]::Pattern);if($null-ne$p){$h=[Windows.Automation.ScrollAmount]::NoAmount;$v=[Windows.Automation.ScrollAmount]::NoAmount;if($dir-eq"down"){$v=[Windows.Automation.ScrollAmount]::SmallIncrement}elseif($dir-eq"up"){$v=[Windows.Automation.ScrollAmount]::SmallDecrement}elseif($dir-eq"right"){$h=[Windows.Automation.ScrollAmount]::SmallIncrement}else{$h=[Windows.Automation.ScrollAmount]::SmallDecrement};for($i=0;$i-lt$amount;$i++){$p.Scroll($h,$v)};return [ordered]@{ok=$true;action=$a;app=$app;element=[int]$op.element;method="uia_scroll";best_effort=$false}}}
    $delta=120*$amount;$msg=$WM_MOUSEWHEEL;if($dir-eq"down"-or$dir-eq"right"){$delta=-$delta};if($dir-eq"left"-or$dir-eq"right"){$msg=$WM_MOUSEHWHEEL};[void][GPTPCWin32]::PostMessage($s.hwnd,$msg,[IntPtr](($delta-band0xffff)-shl16),[IntPtr]::Zero);return [ordered]@{ok=$true;action=$a;app=$app;element=$op.element;method="window_message";best_effort=$true}}
  throw "unsupported action '$a'"
}
while($true){
  $line=[Console]::In.ReadLine();if($null-eq$line){break};if([string]::IsNullOrWhiteSpace($line)){continue}
  try{$r=$line|ConvertFrom-Json;$a=([string]$r.action).Trim().ToLowerInvariant();$session=[string]$r.session_id;$snapshotId=[string]$r.snapshot_id;$mn=300;$md=24;$focusMode="background";if($null-ne$r.max_nodes){$mn=[int]$r.max_nodes};if($null-ne$r.max_depth){$md=[int]$r.max_depth};if($null-ne$r.focus_mode){$focusMode=Focus-Mode ([string]$r.focus_mode)}
    if($a-eq"apps"){$items=@(Get-Process|Where-Object{$_.MainWindowHandle-ne0}|Sort-Object ProcessName,Id|ForEach-Object{[ordered]@{process=$_.ProcessName;pid=$_.Id;title=$_.MainWindowTitle}});Out-Json ([ordered]@{ok=$true;action="apps";apps=$items;count=$items.Count});continue}
    if($a-eq"state"){Out-Json (State ([string]$r.app) $session $mn $md);continue}
    if($a-eq"batch"){$focus=Focus-State (Snap $session ([string]$r.app) $snapshotId) $focusMode;$calls=@($r.calls);if($calls.Count-gt32){throw "batch supports at most 32 calls"};$results=New-Object System.Collections.Generic.List[object];$failed=$null;$batchError=$null;for($i=0;$i-lt$calls.Count;$i++){try{$results.Add((One $calls[$i] ([string]$r.app) $session $snapshotId $mn $md))}catch{$failed=$i;$batchError=$_.Exception.Message;$results.Add([ordered]@{ok=$false;error=$batchError;index=$i});break}};$resp=[ordered]@{ok=($null-eq$failed);action="batch";count=$results.Count;failed_at=$failed;error=$batchError;results=$results.ToArray()};$resp=With-Focus $resp $focus;if($r.refresh_after-and-not[string]::IsNullOrWhiteSpace([string]$r.app)){try{$resp["state_after"]=State ([string]$r.app) $session $mn $md}catch{}};Out-Json $resp;continue}
    $focus=Focus-State (Snap $session ([string]$r.app) $snapshotId) $focusMode;$resp=One $r ([string]$r.app) $session $snapshotId $mn $md;$resp=With-Focus $resp $focus;if($r.refresh_after){try{$resp["state_after"]=State ([string]$r.app) $session $mn $md}catch{}};Out-Json $resp
  }catch{Out-Json ([ordered]@{ok=$false;error=$_.Exception.Message})}
}
