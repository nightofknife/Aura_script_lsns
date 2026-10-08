# 识海深潜识别 Goal（2026-10-02）

> 2026-10-04 更新：用户已恢复研究与实机授权，并暂时取消识别总耗时的
> 验收限制，优先稳定识别。以下90秒口径是历史条件，当前以
> [分离姿态控制实验记录](resonance-pc-deep-dive-front-view-20261004.md)为准。
> 20次同版本真实准确性验收仍保留，正式通过仍为0/20。

用户授权主代理统筹各种方案、子代理理论探索和实机资源审批。单次识别上限从60秒放宽到90秒，连续20次每次准确率必须超过99%。已按用户要求恢复推进，正式验收0/20；Goal工具当前返回blocked且没有代理可调用的resume接口，不将工具状态描述为active。此前不同代码修订的实测不计入正式连续验收。

## 验收口径

- 输出玩家、奇点、全部灵感的类别及所在 `(face,row,col)`；每次关键目标全部正确，漏检、错格、错误附加目标均失败。这些实体数量少，99%以上不能容许一个关键目标错误。
- 同一冻结实现和模型连续20次真实识别。版本/参数有影响识别的变化后重新开始序列，不挑选成功样本。每次独立采集，不复用旧截图或上次结果作为本轮新证据。
- 真值独立于被测模型输出。优先手工多面核对和经独立图案验证的状态变换；被测模型自己的输出不可用于确认其正确。记录棋盘重复与变化的范围，不把同板重复可靠性当泛化准确率。
- 记录任务调用到业务识别完成的端到端 wall 时间、动作 `invocation_elapsed_sec`、扫描核心时间、HUD/初始化/重置/封存时间。每次总识别<=90秒，超时或未完成同样失败。常驻运行环境的建立费用另外记录，不能藏掉每轮必要工作。
- 原先平均<=50秒、通常约40秒仍是优化目标；本次优先验证90秒上限下的识别可靠性，不把90秒当期望耗时。
- 普通格子的事件图案按后续计算需要补读，不能将未读内容猜成已知。需要证明扫描→严格配准→按需补读能支持后续操作；识别评测本身不以真的点击目标作为完成条件。

## 主代理资源管理

1. 子代理只读探索、独立原型，不直接操作游戏；生产改动、训练或实机试验须由主代理明确审批后安排。
2. 同时仅一个流程持有游戏输入/捕获资源。不得并行重置、拖动、点击棋盘。
3. 正式计时和基线实机期间暂停训练、视频大量解帧、pytest与重负载基准，避免污染耗时。轻量静态阅读与文档工作可继续。
4. 运行使用项目内临时目录。结束先释放输入、停止采集线程并关闭测试runtime；仅处置已核验属于本测试的残留helper，不停止游戏或其他Python进程。
5. 每项方案先审查真实来源证据、可实现性、成本及失败边界，离线验证后才批准小规模实机。通过探索阶段后冻结候选，开始20次连续验收。

## 并行探索

|负责人|方向|当前权限|
|---|---|---|
|anchor_vision|方案A：已知glyph局部单假设匹配及格线/角点辅助校正，减少全分类成本|离线理论/小原型，实机未批|
|scan_planning|方案B：多面重叠的面级覆盖，未确认目标局部补扫，避免盲目逐格/强制正对|离线理论，实机未批|
|acceptance_protocol|方案C：多视角目标归格、替代分类/定位方案可行性，20次独立真值协议|只读数据审计/理论，实机未批|
|主代理|审查方案、串行安排实机、整合实现、验收计时和证据|当前持有实机资源|

## 第一项批准的试验

保持现有算法与识别安全门，将显式测试预算设为90秒，检查60秒截断是否是主要漏检原因。生产默认仍60秒；扫描/运行配置最多90秒。该试验只用于基线，不自动计入最终20次验收。

资源及原始结果：`.pytest_tmp/deep_dive_goal_20261002/`。方案报告在其 `research/` 子目录。后续每轮审查和正式验收结果在此文档继续记录。

## 探索记录（不计正式连续验收）

|试验|实现/配置|结果|审查结论|
|---|---|---|---|
|baseline90_01|原cells，CV16/默认ORT2，90秒|识别总计79.312秒，任务wall85.237秒；六面、32普通节点，四个目标坐标符合当前独立变换记录；业务阻塞anchor_recovery_no_current_evidence|延长预算不能解决当前目标消歧与控制触发问题；不能算通过|
|mixed_cpu4_02|混合面覆盖/局部确认、语义revision触发修正，CV1/CPU ORT4|总计44.906秒，任务wall48.445秒；五面、24普通节点；pose_inliers:2/24停止|未通过，继续审查控制/路线；未降低定位或readiness门槛|
|cells_cpu4_03|cells、语义revision触发修正，CV1/CPU ORT4|总计74.844秒，任务wall78.138秒；六面，targets_ready；U11玩家、D11奇点、F22/B12灵感|一次探索成功，不计正式20次；普通节点28个，不能称54格内容已全读|
|cells_dml_04|cells、语义触发修正，CV1/DirectML ORT1.24.4，RTX device1|总计47.219秒，动作invocation50.454秒，任务wall50.487秒；六面、四目标全符合独立记录，104次实际模型执行，无缓存票|当前最优探索候选；全进程测试隔离ORT映射仍需替换为独立worker后再冻结正式版本|
|worker_exploration/live01|真实独立DML worker + 严格验收harness|worker实际ORT1.24.4/DML device1，主ORT1.27保持；两次前HUD完整同值，但黄色旋转1/1需OCR，source_age为1.110/1.187秒，未开始扫描|探索失败完整留档；不计模型扫描失败或正式成功。worker close实际drained=true。获批原生黄色数字模板改善速度，未放宽时效门槛|
|worker_exploration02/live01|独立worker、原生黄色HUD、加强源图审计|dispatch90.110秒被取消，core78.594秒，六面39节点，输出四目标坐标符合独立真值；HUD源龄.062/.063/.094秒且八字段不变|失败。F22两group实际仅7.262度，不能作为独立确认；末尾17.984秒拖动未恢复多面支持。取消时账本比wrapper收尾更早读到空结果，已修drain时序，未改写历史失败|
|worker_exploration03/live01|实际独立正票门、多面恢复源basis、完整drain审计|dispatch90.609秒，core85.422秒；六面42节点，四个目标坐标及实际来源审计均正确；HUD完整新鲜不变|仍失败：最后目标证明齐全后，186个控制采样没有一次语义源龄≤.8秒。最终readiness为真，不能抵消封存超时；新的恢复特例不能解决发布延迟|
|worker_exploration04/live01|每个新的合法checked frame替换单槽|dispatch90.579秒，core85.063秒；六面52节点，四目标坐标正确，但anchor_recovery_failed|失败。平均语义统计源龄.866→.758秒改善；F21仍有历史误票与真实空格票冲突。末尾Boss背面候选未关联且当前map无效，不能凭正确坐标封存|

获批实现为显式实验参数 `scan_route=mixed`，默认仍cells。它优先补真实单票目标的小角度证据；对未归属候选保留消歧要求，不按历史目标数直接结束。语义revision更新会触发策略重新审查，但只有新geometry source才能重新获得输入额度。

已知图案子集分类在181个真实裁片上输出一致，但没有实质加速，不接生产。CPU线程离线四帧比较以CV1/ORT4最优，仅作为实机配置候选，不将离线计时当端到端表现。GPU运行库存在CUDA13依赖缺失，目前不替换生产环境。

已有目标解释器原型仅解释有至少两次独立正面实证支持的当前背面精灵，不增加positive vote。61个真实Boss候选的数学重放中，21个背面D11解释通过，两个误投影D00未通过；当前HUD不变与时效性在重放里是假设，因此尚未接入生产。需要先取得同源实时HUD证据。

验收工具 `tools/deep_dive_acceptance.py` 已完成31项单元验证，记录冻结指纹、预注册独立真值、双耗时、连续20次与失败归零。工具不能独立证明WGC声明，也不能代替手工真值审查。目前仍未冻结候选，正式0/20。

## 当前获批后续工作

- 将DirectML放入独立子进程，主进程OCR/其他计划保持原ORT1.27；完整RGB/request ID/hash及模型指纹绑定，CPU默认不变，explicit DML失败不得静默回退。主进程保留原解码、弱框、局部头融合、54格竞争与readiness。
- 正式验收harness只使用预先冻结ledger及独立注册truth，常驻runtime复用；每轮完整同源HUD开始两读、结束一读，全部费用计入90秒。失败完整记录并归零，不挑成功样本。
- 14张原始多视角审查包位于research/oracle_review。候选格网默认关闭；图案连续关系与目标实际基座仍需逐项人工核验，不能把自动格网当真值。
- 输入确认原型24项数学/状态单元通过，未接实机。直接启用会因频繁姿态修正和微小尾债卡住，暂不替换成功候选控制器。mixed路线保留实验；当前默认cells。

已人工复核原图及多面连接，独立真值保存为 `research/oracle_review/reviewed_truth.json`：玩家U11、奇点D11、灵感F22/B12。注册在探索ledger中，范围仅一个棋盘状态；后续须扩大真实状态覆盖。验收harness冻结完成，60项工具/ledger测试通过；完整深潜测试844项通过（55.08秒），worker模块入口修正后24项窄回归通过，包check通过。正式连续验收仍0/20。

独立验收审查进一步要求：冻结实际计划配置、glyph/HUD模板、OCR模型元数据与ONNX、主ORT二进制及隔离DML二进制；证据来源包括实际使用的负票/几何来源；不同正票须不同原子帧身份及真实至少8度SO3视角差，不能以组编号替代。当前harness正按此补齐，完成后重新冻结新探索ledger，保留live01失败不覆写。完整HUD每次读取仍要求真实同源且未过时，不用动作旗标推断黄色quota数字。

黄色quota原生读取修复已通过48项窄回归。保持字形置信度 `.76` / margin `.045`、完整组件、唯一slash、行距/数值范围门槛，只在rotation固定ROI加入真实黄色前景并辨别独立左侧圈勾几何；勾本身不提供数字。两张真实全图的八字段与先前OCR完整结果相同，冷读取106毫秒、热11.5毫秒，无OCR请求。这是离线读取成本，不是新的实机识别计时。

替代模型静态审查维持以下边界：四类格子实体CNN在旧独立会话仅122/153正确，另一次live材料120/159，灵感漏/误判严重，不能替代当前全图实体定位。七类图案CNN的35张测试缺白/紫类别，虽批27格约1.98毫秒但不能主张泛化；现有规则分类约46.5毫秒也不能直接换算总扫描增益。共享全图backbone输出图案中心及实体框有长期研究价值，仍需全图真实标签、按棋盘/轨迹划分及严格归格守卫。当前20次候选不引入未验证模型。

探索02后修正已冻结：mapper目标确认须真实SO3正票对相隔至少8度，并优先保存证明对；强锚抑制须六张实际独立视角。恢复过程以真实多面支撑源作导航，新增实际已应用body修正累计basis，跨epoch右乘变换历史导航姿态，历史姿态不能成为当前证据；强化恢复须新同源至少六glyph、至少两面各两glyph的原严格续锚及真实到达。取消后先等wrapper收尾再审计，取消仍失败；冻结文件校验费用单独披露，不伪称任务dispatch。扫描线程实际drain后的成本在`stream_statistics_drained.json`保留。联合完整深潜测试931项通过（57.31秒），包check/validate通过。正式0/20。

探索03末尾22个语义源的采集到处理开始平均等待.496秒，语义工作平均.549秒，发布源龄平均1.045秒；其中等待包括跟踪、页面检查及队列，不能从现有日志精确分摊。批准最小调度修复：每个新的、同源通过玩家棋盘检查的包更新容量一的语义队列，取消原先页面提交的.25秒/8度节流。消费者仍单线程、原执行节流不变，真实8度独立正票要求不变。静止连续新源替换测试及页面中断/旧源/跨会话测试共5项通过。下一次探索单独验证该变更，不预先宣称解决时效。

探索04后新候选（尚未实机验证）：

- 扫描专用页面短路：当前帧用原模板/阈值逐项否定高优先级页面，同时获得player-turn、heading、plane正证；非healing/未知/交互/弹窗/异常回完整observer。公共动作仍用完整observer。44项合同回归通过；254张真实图全部非分数输出相同，离线43.28→36.14ms，不能把16.5%直接当实机收益。
- 私有结果深拷贝移出反馈锁，冻结一次后构造控制投影；snapshot仍给消费者独立副本。源龄统计明确改为实际发布时刻（含构建费用），不再仅报fusion结束时刻。标注显式复用同轮融合结果，默认annotate行为不变。
- 新正票守卫：目标所挂面须有本帧至少2个真实glyph支撑；全54法线corridor须在半径sqrt(.5)像素的量化圆盘上始终满足原error<.30/margin>.12。全圆盘Lipschitz证书只保证固定几何及给定点域，不冒充模型/姿态误差界。167个保留语义源的投影统计拒全部3个F21误归属，保留四真实目标的实际8度pair；11个保存源投影回归通过，不计实机验收。
- 背面候选解释：semantic owner先用原RGB/真实WGC packet准备2源完整HUD基线，后续当前像素/变化ROI原生数字读值证明8字段未变；模型/glyph后仍须同源且≤.8秒，严格已有独立前面正票及54竞争，通过才解释当前候选。失败保持原result，不增票、改格子或覆盖前面关联。相关117项回归通过。
- 灵感红色螺旋实际属于背景格图案，不能当实体固定中心。黄色环椭圆原型仅2/17（增加6px上下文后3/17）可靠候选且两误归格仍不确定，不接入当前候选。

以上实现已组成候选05完成实机探索：dispatch 80.796秒、core 76.109秒，六面43个普通节点；玩家U11、奇点D11、灵感F22/B12全部符合独立真值。四个目标均有实际不同帧、不同group、至少8度的正票证明；最终发布源龄.796秒。此次是探索，不计正式验收。

随后冻结同一候选05开始正式验收，第一轮失败即停止：dispatch 49.328秒、core 45.062秒，五面22节点，仅玩家确认；失败原因为anchor_recovery_no_current_evidence。未超时，但业务未就绪、目标集合不完整。正式尝试1次，连续通过0/20，原账本及失败材料均保留。

失败恢复使用了最新的L7/B2源，其中B2全部是新图案，不能在后续known-only搜索中提供支撑；附近搜索反复停留在单面L。候选06增加最多12个真实历史支撑姿态的导航库，优先保留有至少两面已知图案的源，薄侧新图案不能挤掉可重现旧源；附近搜索须预测已知多面支持，否则改用另一个可达真实历史源。历史只用于导航，仍须当前新帧多面glyph、原时效、到达角度和4秒恢复总预算才可成功。待独立审查及完整回归通过后重新冻结并实机测试。

不放宽正票裁片边界：早期F22画面134/586的精灵实际被底部HUD部分遮挡，不能因关联坐标正确直接接票。BLAS单线程和JSON深拷贝替代研究没有足够收益，未接入生产。当前游戏棋盘状态未改变，最终验收还需覆盖更多真实状态。

候选06完整相关测试1039项通过，包check/validate通过。单次实机探索仍失败：dispatch 38.891秒、core 35秒，四面18节点，仅U11玩家/F22灵感确认。历史源库有效选到了真实旧多面源，但恢复途中新鲜多面源出现时尚未满足旧goal的4度到达条件；真正到达后源龄已超过.9秒，随后不必要地切换12度局部搜索，4秒总恢复预算耗尽。该轮完整保留，不计正式20次。

候选07研究方向：把恢复成功绑定到真实新支撑源与当前几何直接相差不超过4度，而非要求到历史导航goal后才承认；新源同会话/地图/帧身份、至少6glyph/两面各2、原时效和预算仍保留，最终目标readiness独立保持。另加入纯诊断阶段计时，区分模型检测、glyph拟合、图案对齐、融合、HUD与发布构建费用。相邻顶点URF→DRF→DLF→DLB的两面重叠路线另做opt-in原型，未默认启用，不能以理想几何覆盖宣称实机可用。

## 候选07/08的实际结果与当前方向

|候选与场景|端到端秒|结果|
|---|---:|---|
|07单次探索|88.625|四目标、真实独立来源、完整HUD及封存全部通过；探索不计正式验收|
|07正式首轮|40.188|恢复失败，仅U11/B12；首失败停止，正式连续0/20|
|08 cells探索|90.578|超时，U11/D11/B12，F22未达独立确认；六面48节点不能代替目标闭合|
|08 vertices探索|89.094|仅U11单来源、三面7节点，启动known多面支撑不足形成静止死锁；没有成功进入顶点路线|

07真实计时主项每帧平均：模型检测139ms，glyph拟合/对齐221ms，HUD95ms；复制与当前候选解释约16ms。08将HUD重读限定为实际变化ROI，保留完整8字段、当前逐字节相同ROI证明及原native组件/置信/时效门。138真实素材中两次原生基线后135帧一致通过，一帧rounds未知一致拒绝；离线完整17.790ms/局部10.006ms。08实际HUD平均53.828ms，观察上降低43.4%，但两轮路线不同，不称严格A/B。08尾部语义出队前平均等待371ms，抵消部分收益。

08恢复等待修改：真实多面源仍支持已到达当前姿态、仅发布过期时停止输入等待新源，不在.65秒后盲搜12度；原4秒总预算与fresh退出门不变。08的F22到83.328/83.875秒才留下两张近角正票，未满足实际8度pair；此前仍在补D/L普通格子。下一方向是cells全局覆盖加真实未闭合目标优先的局部确认，以及semantic取最新完整geometry packet并对其RGB重新核验scene，禁止旧scene贴新RGB。vertices另修真实bootstrap与新面补采，仍opt-in。

08联合深潜1093项测试通过，包check/validate通过。所有实机runtime均已确认释放输入、drain关闭，仅关闭自有helper，未退出游戏。当前真值仍同一个棋盘；正式20次验收及跨状态覆盖尚未完成。共享同crop HSV原型在225真实裁片中完全等价，但每源仅节省1.95ms，未接生产。

## 候选09：最新同源语义入口与正式失败记录

候选09启用显式`refresh_semantic_source=True`，仅在冻结的原生`scan_scene.observe`入口生效；默认及自定义observer保持原行为。语义消费者选择更晚的完整WGC包，校验帧/generation/采集时间严格递增、会话/地图一致、实际RGB及合法姿态/body basis，并对新RGB重新运行完整原生scene谓词，再准备同源HUD和模型识别。跨源、重复、停止、异常或非玩家棋盘包不进入模型融合。旧scene不能贴到新RGB，inline检查也不更新独立scene owner的页面、输入和封存授权。真实8度正票、目标面本帧glyph支撑、HUD及源龄门保持不变。相关联合深潜1143项测试和包check/validate通过；单元结果不计实机准确率。

|路线与阶段|dispatch秒|结果及验收处理|
|---|---:|---|
|target_cells探索|89.906|59次重规划，五面20节点，仅U11确认；业务不就绪，失败，探索不计正式验收|
|cells09探索|76.438|U11/D11/F22/B12全部符合独立真值，业务及来源审计通过；探索不计正式验收|
|cells09正式第1轮|55.422|core50.734秒，完整目标集合及审计通过，连续计数到1|
|cells09正式第2轮|90.265|core87.328秒，六面44节点，仅U11/D11/B12，缺F22；业务未就绪且外层双计时超过90秒，连续计数归零|

正式材料保存在`.pytest_tmp/deep_dive_goal_20261002/acceptance_candidate09/state01/round_0001`和`round_0002`，失败原账本与源图完整保留。第2轮不是仅封存年龄问题：F22最终没有任何格子证据，拒票发生在两层。帧125/157/167/394（14.859/17.031/17.812/33.140秒）已有实际续锚、F面两个glyph和全54竞争/量化证书，mask关联成功；但格子完整quad均越过可读区下界y615，`_observe_cells`未接正票。125/157/167还存在HUD遮挡风险，394实际cosine约.298，低于原.30门。157与167仅相差2.637度；125置信度.6586不足.70，394也不满足cosine，所以仅删完整quad门仍不能证明获得合法8度pair。

另有14次明确的F22关联阶段拒绝：8次本帧F面glyph数量为0，被`target_face_glyph_support_required`拒绝；6次本帧glyph姿态未续锚，被`same_frame_glyph_pose_not_renewed`拒绝。它们不是全部F22候选，不能把缺F面支撑当作唯一原因。目标优先调度只接受实际正票，早期成功mask关联没有入票，因此没有触发局部确认；末段75.984至85.875秒的候选仍缺合法同源支撑。背面解释器只解释已有独立正票确认的实体，不能补出F22的新票或绕过面支撑。详细只读诊断保存在`research/candidate09_round2_f22.md`。

第2轮保存的136次语义识别全部实际刷新了来源。完整每帧均值：模型检测135ms、glyph拟合142ms、atlas对齐65ms、语义总计393ms；额外inline scene101ms、HUD45ms、复制/解释/控制投影合计约25ms。末尾256个控制采样里的17个独立发布源，采集到处理开始平均123ms，融合677ms，发布准备27ms，实际发布源龄826ms（最大984ms）。相较08尾段371ms等待，最新包入口减少了等待，但重复scene核验和原有检测/几何成本仍消耗新鲜度。可靠性优先方向是把严格同源mask关联作为有界导航提示，将目标格子与精灵移入完整可读、无遮挡且cosine合格的位置，同时保留F面至少两个glyph，再实际采集8度证明对；导航提示不能变成正票。该修复尚未实施或实机验证。速度方向仍是检测、拟合/对齐和重复scene开销，不延长预算。

场景限速到250ms目前仅作理论研究：第2轮尾部scene完成间隔均值148ms，scene源龄均值336ms、p90为422ms、最大531ms，已有两次`scene_feedback_stale`。直接增加约100ms调度等待可能越过原.5秒场景门；节省CPU竞争是否足以抵消需另做受控验证，不能仅以调用次数减少断言安全或收益。当前未实施此限速。

上述实机runtime均确认关闭、自有子进程drain无错误，未关闭游戏。当前正式连续通过为**0/20**；同一棋盘上的探索成功及正式首轮成功不证明20次稳定准确率，也不证明跨棋盘泛化。下一候选须重新冻结，继续完整记录所有失败与真实双耗时。

## 候选10/11失败与候选12冻结准备

|探索路线与版本|dispatch秒|实际结果|
|---|---:|---|
|四顶点10|88.891|仅U11确认；剩余约54秒进入空转。后续新增永久回退cells，但该回退尚未重新实机验证|
|target_cells10|90.656|仍缺F22，超时失败，不能计正式成功|
|target_cells11|13.125|启动早期bootstrap锚失效即失败；尚未检验到目标提示调度的效果|

四顶点10末尾HUD原图数字肉眼清楚，但原生rounds完整组件匹配仅.2897，因此拒绝完整八字段证明是合理行为。只读像素审查发现顶部16个新增青色装饰像素连入数字6，拉宽组件；数字主体与同轮初始图一致。没有缓存旧值、猜数字或降低匹配置信度门。不能用肉眼读值覆盖该轮自动验收失败。

候选12已实施以下变更，尚不能主张实机达标：

- 显式opt-in `startup_fast=True`仅用于targets扫描，harness配置严格要求布尔值。启动观察仍绑定实际同源RGB/帧身份，保留原6秒启动预算，不以配置开关代替页面或glyph证据。
- 严格关联但未入正票的目标mask可提供有界导航提示：保存实际关联来源，最多12秒、两次尝试，不造正票。移除额外hard-known端点预测限制，导航改用原Cell potential；实际目标确认、可读区/余弦/面glyph/8度来源门以及4秒恢复预算不变。
- 末尾HUD首读字段未知时，最多再取两张新的实际WGC帧，运行原完整八字段原生读取。每次原图、读值JSON、源身份与文件/RGB哈希、耗时全部保留；仅来源、页面、原生已知字段和新鲜度均合法的未知值可重试。首次任一已读字段与基线变化即立即失败，不能选择后续恢复相同值掩盖变化。来源不匹配、过期、非原生或非玩家棋盘不重试。
- HUD重读严格处于原`dispatch_at+90秒`期限内，读图、处理、保存及审计均计时；剩余不超过.5秒时不再启动一次完整采样。重读失败或任何超90秒仍由原ledger判失败并归零，不延长期限，也不把post检查移到识别计时之外。

隔离FP16研究没有替换生产模型。20张真实源图使用相同完整ROI/640预处理、两独立DML worker及原解码/弱框/局部头融合，执行均值8.531→5.491ms，但完整检测端到端仅57.520→55.198ms，平均省2.321ms。54个框无增漏，中心仍可偏移.4303px、整数bbox偏移1px，原始输出还有.05/.6门跨越；未证明严格几何归格等价，更不能证明99%准确。收益不足以支撑此次切换，生产模型SHA仍为`bff7444233525475ef25173845a6c197c1eec993f2544d204a44846260718123`。

候选12联合回归1242项通过，包check/validate通过。候选10/11都是失败的探索记录，不能并入正式连续通过；正式仍**0/20**，跨棋盘状态覆盖仍未完成。

## 候选12/13：真实取景不足与下一次实验

候选12在任务输入校验阶段拒绝`startup_fast`，dispatch仅.016秒，没有开始模型或游戏输入。主代理补齐公共action、任务YAML及生成manifest的布尔参数传递，并增加真实InputValidator与公共action转发边界测试。相关111项检查通过。原失败账本保留。

候选13启用修正后的fast startup：dispatch90.344秒、core84.890秒，实际确认U11玩家/B12灵感，漏D11奇点/F22灵感，超时失败。运行关闭且worker实际drained=true。第一张stream语义源frame10的glyph续锚时间与真实采集时间完全一致，证明已逃出候选11的启动卡点；此结果不证明路线成功。

逐帧源图与实际拟合姿态审查发现：F22在18个cosine>=.30的姿态中，完整quad全部落到安全区下界615之外；D11在114个有限姿态中cosine最大仅.15286，且没有当帧D面glyph支撑。强Boss框虽然可见，所属D面仍在背面或近侧边。这两处均没有正票，不能通过增加计票或放宽归格门修复。当前路线将部分上排已观察的F面视为已访问，转而追L/B/D，没有保证下排目标区真正进入可用镜头。

下一轮分两项独立审查：路线按真实新鲜语义源的逐格完整取景机会安排覆盖，取景机会仅用于导航，不能生成occupant/none/vote；速度将同一私有RGB的局部玩家Hough检测与隔离DML请求重叠，保留原解码、融合、候选与阈值。新增线程必须单帧单任务，异常及close均实际drain。两项尚未完成实机验证，不称已获得速度或准确度收益。主代理独占实机/输入与GPU实验批准权，子代理只作分工源修改、离线检查和理论审查。

候选13诊断5Hz线程采样属于探索工具，不能计入正式20次；正式当前仍**0/20**，当前棋盘真值未变化。原始失败记录位于`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration13/live01/round_0001`，只读诊断位于其`research/candidate13_targets_startup.md`及`research/target_cells13_route_review.md`。

## 同源局部头检测与DML重叠：离线证据

已实现仅DML worker+hybrid路径的单帧重叠：一个自有单线程future读取同一张私有只读RGB进行原局部玩家头检测，主调用同时执行原FP32模型请求。返回后仍用原解码、NMS、融合和弱框规则，不删候选、不改阈值或模型；CPU、禁用hybrid及完整RGB缓存路径保持原行为。模型、解码或头检测异常均失败，并在释放服务锁前等待本帧future结束；close必须真实shutdown/drain自有线程。运行中的原生Hough无法强行取消，不能以发出cancel声明已经drain。

主代理独占GPU批准并执行20张候选13真实源图的离线对照，使用相同FP32模型/完整ROI/640预处理及同一个隔离DML worker，交替顺序比较原顺序与重叠路径。所有20个完整packet逐值相同；平均端到端50.241→33.011ms，节省17.230ms（约34.3%），中位数50.239→32.880ms。实际ORT1.24.4、DML device1/intra4；原模型SHA不变，主ORT模块在实验前后均未加载。总实验3.959秒，模型worker及新增头线程均确认drained=true、关闭后不再owned/in_flight。证据保存在`.pytest_tmp/deep_dive_goal_20261002/research/head_overlap14/paired20.json`。

上述为真实保存RGB上的离线推理等价与耗时，不是实机端到端计时、99%准确率或20次正式识别证明。harness补充新线程的收尾审计：出现head_thread.close时要求drained=true且owned/in_flight严格false；未创建线程的CPU、非hybrid和旧状态兼容。失败写入原`runtime_closed.json.drain_error`，不改变原90秒计时或ledger判断。仍须冻结后单独实机验证资源竞争下的速度、源龄与完整目标闭合。

## 候选14实机与精确ROI缓存审查

候选14实际dispatch 90.907秒、core 85.094秒，超时失败；U11玩家、D11奇点和B12灵感已确认，F22仅frame1032/group44一张合法灵感正票（confidence .871、cosine .302、原all54几何通过），缺少不同实际帧且相隔至少8°的第二票，继续unknown符合原确认门。最终6面、49known、128次semantic、655次scene、1033次tracking；11次replan，其中4次opportunity navigation。模型worker与自有head线程均真实drained，关闭后owned/in_flight=false，drain_error=null。正式连续通过仍为0/20。

重叠在实机确实启用。保存semantic阶段平均detect为93.453ms（13为179.252），refine 175.644ms（13为135.912）、atlas 73.975ms（13为60.902），总semantic 411.841ms（13为438.733）；14棋盘已知格更多，不能视为相同工作量的严格A/B。semantic FPS 1.501→1.646，整体平均发布源龄791.319→725.219ms；末尾日志保留的16个独立来源却平均862ms、最大968ms，13个超过.8秒。当前速度改善没有解决末段新鲜性及F22有效视角覆盖。逐帧head/worker细项未保存，末次服务状态只能证明该次：原模型forward 22.061ms、IPC 83.748ms、等待head 34.865ms、service total 123.508ms；不能由末次值声称整个回合的平均GPU或头检测费用。只读汇总在`.pytest_tmp/deep_dive_goal_20261002/research/cost14/summary.json`。

未采用新增ROI缓存支线。现有page/single/battle/event模板匹配已以当前ROI字节与实际模板/函数依赖作精确缓存；23张候选13真实保存RGB的离线CV1检查中，最重的insufficient_roles_followup ROI覆盖动态魔方，22个跨源比较均不相同，起始0000–0008的8个相邻比较也全部变化。整个魔方ROI、tile/arrows与rest-modal区域同样动态。未缓存的event.heading_present约1.38ms/图，仅8/22比较完全相同，预计收益小。同一RGB立即重读38.03→15.14ms且23份完整场景输出相同，证明已有缓存能省同图重复；inline选用更新真实来源，不能把该离线重复收益当作实机跨线程收益。未修改缓存、场景判定或阈值，证据在`research/scene_exact_roi14/{summary,adjacent}.json`。

## 候选15提前恢复失败

候选15冻结前联合1313项测试通过；实机实际dispatch 33.797秒、action total_elapsed 30.922秒、layout扫描elapsed 29.672秒，以anchor_recovery_failed提前停止，未触90秒超时。仅U11玩家与F22灵感确认，D11奇点/B12灵感仍缺，17known/3面；因此业务失败，不能计入正式通过，连续结果仍0/20。收尾模型worker及head线程drained=true、owned/in_flight=false，drain_error=null，52次实际模型执行、2次缓存命中。

本轮47次semantic、226次scene、300次tracking；semantic FPS 2.118、平均发布源龄577ms。保存帧平均detect 80.961ms、refine 120.295ms、semantic total 286.507ms、inline scene 98.568ms；末次原模型forward 23.562ms、IPC 58.813ms、等待head 13.423ms、service total 85.710ms（650个pink component/64个局部window）。这些是不同短回合的实际观测，不是与14相同路线的A/B。停止前timeline中当前geometry age .172秒、scene age .250秒、glyph/semantic来源age .844秒；最后semantic发布时age .641秒，current geometry与scene仍tracking_ok，不能把平均发布源龄当作停止时可用的新锚。

另有独立harness审计误拒待修：记录state_unchanged=false、hud_raw_source_mismatch，尽管保存post PNG文件SHA、RGB SHA与JSON都相符，原8字段native HUD与两次pre完全相同且post读取source age .187秒。静态定位到post_hud_attempt_reason直接比较JSON读取值与内存sample；原native glyph alternatives来自sorted(dict.items())、包含tuple，JSON序列化后是list，真实同源数据会因表示不同而拒绝。该问题未在此次只读审查中改源码，也不改变15本身的anchor失败和目标缺失；后续需保留全字段、PNG/RGB哈希、实际来源与时间门，修正JSON表示比较并加入真实tuple回归，再收集新正式回合。证据目录为`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration15/live01/round_0001`。

## 候选15：历史导航信息与当前识别时效分离

候选14的早期实际frame124已有强F22关联（confidence约.765、U2/R3/F5续锚、ownF5、原全54格量化证书通过），但完整quad下边约652，仍不能计正票。该源的保存语义年龄为.859秒；根据后续控制反馈时间推算，控制端首消费时已约.906秒，刚越过导航提示的.9秒门。消费时刻只有保存时间推算，没有完整逐次内部快照，不能把.906当作精确实测字段。F26没有被保存为提示，全轮仅4次机会导航评分，最终到83.719秒才有第一票。

候选15仅拆开这两种用途：严格同源、续锚、多面glyph、本面至少2glyph和原全54格证书成立的坐标，可在年龄[0,2)且未fusion_paused时创建有限的历史导航提示；同格只创建一次，12秒期限、最多两次尝试不能因重复或新源重置。已验证的历史提示和未取景bitmap用于当前摄像头候选评分，不再要求每次消费都重获瞬时模型源资格。新取景bit仍要求原<.8秒实际源；正票、局部确认源、输入、恢复、readiness及最终封存全部保持原门。跨上下文即使缺少有效body basis也先清旧导航提示。

候选14已完成1303项联合回归。候选15已完成89项聚焦回归、独立静态审核及1313项联合检查；冻结后的实机dispatch 33.797秒以anchor_recovery_failed提前失败，详情见上节，当前正式仍0/20。主代理未重复原14配置，所有失败记录保留。详见`research/candidate14_targets_pose.md`和`research/candidate15_history_navigation_review.md`。

已批准并修复post-HUD的JSON表示误拒：对15保存原图重放原native observe/read_hud，内存返回与原保存JSON只有33处list/tuple差异，没有键、长度或数值差异，严格JSON规范化后完整相等。审计现在仅在比较保存JSON时使用json.dumps(..., allow_nan=False)再loads；不使用default=str，不改返回sample或其源时间，非有限/不支持对象仍失败，原PNG文件/RGB哈希、完整八字段、native来源、源身份推进、新鲜度和90秒期限保持。新增真实native tuple结构及字段、来源、保存内容变化反例；post-HUD/harness/head-drain聚焦112项测试通过（2.95秒）。离线表示证据在`research/post_hud_tuple15/representation_audit.json`；没有重基为实机fresh证据，也没有改写15失败或计入正式20轮，修复后的新实机仍待验证。

## 候选16：新正票成立，但缺面与末尾预算未闭合

候选16冻结前联合1342项测试通过。实机dispatch 89.860秒、invocation 89.813秒、wrapper 89.281秒、action total_elapsed 87.109秒、layout扫描elapsed 84.922秒，以time_budget_exhausted结束；原reserve_used=true。业务与目标readiness均失败，确认U11玩家/F22灵感，仍缺D11奇点/B12灵感，最终30known/4面，D、B各0个已知节点。正式连续通过仍0/20，不能仅凭dispatch略低于90秒计成功。

两处实际目标确认有独立正票：U11原始32票、保留6条，frame899/group30/confidence .816与frame243/group11/.813的实际SO3间隔60.3406°；F22原始9票、保留6条，frame803/group25/.943与frame672/group24/.927间隔11.3168°。上述不同实际帧、不同group、同map0，超过原8°门；不把原始票数直接当独立票数。匹配glyph定位的失败后单次重试在frame374真实发生，9个候选/9个known匹配，原seven_inliers_missing恢复为seven_centres并renewed；该次定位恢复不替代实体正票或最终封存证明。

末尾post_hud_attempts为空、post_hud_elapsed为0，harness按原dispatch+90秒期限及最低.5秒采集/处理余量拒绝继续采样，记post_hud_dispatch_budget_exhausted。没有采到post-HUD，state_change_unresolved表示未证明状态不变量，不能声称观测到HUD已改变。本轮未发生tuple误拒，但也未执行新JSON比较，不能当作其已通过实机验证。所有旧失败结果保留，未延长deadline或将post采样移出计时。

本轮125次semantic、710次scene、1034次tracking；平均semantic发布源龄732ms、semantic FPS 1.622。保存帧平均detect 93.295ms、refine 190.900ms、atlas 80.802ms、semantic total 428.366ms、inline scene 99.129ms；末次原模型forward 21.872ms、IPC 53.704ms、head等待26.974ms、service total 84.206ms（475个pink component/83个window），末次不是全轮平均。停止前current geometry age .063秒、scene .266秒、glyph/semantic来源 .703秒，tracking均合法，仍无法补出D/B缺失证据。收尾模型worker与head线程均drained=true、owned/in_flight=false，drain_error=null，130次实际模型执行、3次缓存命中。证据目录为`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration16/live01/round_0001`。

尾预算审计：由timeline的at/elapsed推算core start约30789.562，原dispatch start为30786.890，前置约2.672秒；result的elapsed字段在约30874.484赋值，action total字段约30876.671赋值，差2.187秒。源码该区间包含结果元数据、34张格子crop保存及report/layout输出（layout约9.04MB）；无细分计时，不能将2.187秒全部归于JSON。末次controller到result stamp另约.578秒，包含drain/final gate/result准备；两段约2.765秒，已吃掉原采集deadline提前3秒预留的大部分时间，action返回前距dispatch硬期限仅约.219秒，末尾观察剩.140秒，不能再满足.5秒最低post采样余量。reserve_used的原定义只是action总耗时超过50秒优化目标，并非HUD专属预留。该算术解释末尾证据未采到，不改变D/B缺失这一业务阻断或90秒期限。

## 候选17：机会评分关闭的对照与真实post-HUD通过

候选17内部严格bool参数opportunity_navigation默认False，实际bitmap仍按原同源门登记，仅不参与Cell pending/weights/seen/endpoint覆盖评分；historical assigned hint优先、12秒/两次尝试和原局部任务、正票、恢复、封存门保持。显式True保留候选16固定9格对照，不新增外部配置或放宽tail时间。冻结前联合1348项测试通过；实机summary记录enabled=false、29个机会bit、opportunity plans=0，确为该对照。

本轮dispatch 82.125秒、invocation 82.047秒、wrapper 81.562秒、action total_elapsed 79.125秒、layout扫描elapsed 77.157秒，以anchor_recovery_no_current_evidence失败，未触90秒超时。18known/5个observed面，仅U11玩家/F22灵感确认，缺D11/B12；failures为business_not_ready与entity_set_mismatch，正式连续通过仍0/20。玩家原始10票，保留正票中frame676/group7/.813与frame99/group9/.809实际SO3间隔14.6341°；F22原始6票，保留正票中frame276/group13/.939与frame779/group16/.936间隔35.9307°，均不同实际帧/group、同map0。99个来源通过source审计、两个target_source_proofs成立只是来源与这两处确认的证明，不能称99%识别准确率。

末尾真实执行一次新的WGC post-HUD采样（generation4267、source age .204秒、总处理.281秒），PNG文件/RGB哈希与保存JSON、attempt相符，原八字段native值与两次pre完全相同。新完整JSON-native比较实际通过，reason=eight_hud_fields_stable、state_unchanged=true、harness errors为空，未再发生tuple表示误拒；它解决了审计误拒，未使业务识别成功。末尾geometry age .109秒、scene .234秒且tracking合法，glyph来源却已3.953秒，无法当作当前续锚证明。130次semantic/604次scene/874次tracking，平均semantic发布源龄655ms；保存帧平均detect 81.925ms、refine 147.601ms、semantic total 340.894ms，属于不同路线的本轮观测。135次模型执行、2次缓存命中，模型worker与head线程均drained=true/error=null、关闭后owned/in_flight=false、runtime_closed.drain_error=null。证据目录为`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration17/live01/round_0001`；后续仍需解决实际恢复证据与缺失目标，不能合并跨版本回合充作20次验收。
## 候选18：真实单票的局部规划遮挡与橙三眼像素支持

候选18保持opportunity_navigation默认False，只在positive局部任务的私有规划副本中，将同source/frame/map/time/context、经实际body basis转换后与任务origin一致的真实正票设置为task.kind；完整bbox及原sprite footprint可测时，原有限高度遮挡预测才参与局部候选评分。实际atlas的unknown类别、计票、正票证据、全局Cell覆盖、历史hint预算和readiness不变。frame/time/map/rotation/kind/bbox/quad/context等负例仍返回原rows，不让导航预测生成实体票。

另仅将layout中orange_triple_eye的前景mask改为原形状分类器已经使用的warm HSV范围，修复实际833/846/859高置信三眼分类却没有可测像素的矛盾；其他六类mask、同类置信度、实体遮挡、组件定位及拟合门保持。833可补出D22测量支持；846/859仍因支持不足不更新姿态。旧oblique630回归新增的D10是原分类器orange_triple_eye/.815与既有atlas同类，禁局部定位时四点仍无共识、不更新；启用局部定位后保留原median≤3/max≤6/angle≤4/translation≤12/depth≤.035界限。实际823与833仅相隔7.537°，不能据此宣称独立8°正票已经成立。更新两处过时的支持计数断言后，联合1379项测试通过。

冻结后的探索18实际通过一次：dispatch 84.078秒、invocation 84.047秒、wrapper 83.578秒、layout core 78.422秒，status=targets_ready、scan_success/business_ready/targets_readiness均true，44known/六面，实体集合完整且精确为U11玩家、D11奇点、F22/B12灵感。109个实际WGC来源均通过审计，同map0；保留正票置信度最低分别为.806/.959/.885/.902，四实体各有不同真实帧/group的≥8°SO3见证（最小合格角分别8.4772°/8.4574°/9.8152°/9.7196°）。当前C诊断no_strong_unassociated_current_candidate，没有靠解释增加正票。

末尾新WGC generation4336的post-HUD实际source age .172秒，完整原生八字段与两次pre稳定；处理.219秒，harness_errors/failures均为空。原模型主ORT未导入，CV threads=1；worker与owned head线程关闭后drained=true/error=null、head owned/in_flight=false、runtime_closed.drain_error=null。120次semantic、570次scene、870次tracking，平均semantic发布源龄.710秒；这些仅是本轮测量，不能当作同路线A/B收益。

证据为`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration18/live01/round_0001`。该轮属于探索campaign，通过不并入随后正式campaign；开始formal18 count3时正式仍0/20，正式结果以其独立ledger为准。当前同一已知棋盘的一次通过不证明泛化，也未实现连续20次验收。
## 候选18正式首轮失败：探索通过未能重复

随后formal18配置要求count3，首轮即失败并自动停止，实际没有继续采集第二、第三轮；与探索18的code/model/config fingerprint完全相同，正式仍0/20。dispatch与记录invocation均90.297秒、wrapper 89.734秒、layout core 85.188秒，扫描time_budget_exhausted/partial。U11玩家及F22/B12灵感正确，D11奇点缺失；40known/六面，114个实际来源均通过来源审计，但target_source_proofs只包含这三个实体。最终D11没有保留的奇点正票，不是已存在合格Boss独立pair却漏导出的情况，来源合格不代表四目标识别成功。

完整failures为business_not_ready、state_change_unresolved、over_limit_dispatch_wall_sec、over_limit_invocation_elapsed_sec、entity_set_mismatch。末尾post_hud_attempts为空，harness报告post_hud_dispatch_budget_exhausted/public_dispatch_budget_exceeded/task_not_terminal_success；state_unchanged=false表示没有剩余时间完成post证明，不能解读为实际观察到HUD改变，也不能忽略失败或把post移出90秒计时。owned模型worker与head线程均drained=true/error=null，head关闭后owned/in_flight=false、runtime_closed.drain_error=null。证据为`.pytest_tmp/deep_dive_goal_20261002/target_cells_formal18/live01/round_0001`。

目前优先诊断同冻版识别稳定性，暂不执行跨状态操作。探索一次通过和正式一次失败均保留，不能用探索轮替换正式失败或声称连续验收已经达标。
## 候选19准备：同源局部导航预算不随激活重置

候选19针对相同真实正票在冷却后反复激活局部确认的问题，按session/map/kind/cell/frame/time登记实际源预算；只有原局部路线成功创建后才计账，同源累计最多两条路线，重新激活或重置局部attempt计数不能补回预算。新帧要相对该实体此前登记的实际base_rotation均达到原8°间隔才获得新预算，body basis矫正或时间戳刷新不能冒充真实新视角。已在执行的第二条路线保留原任务signature直到完成或原时间/来源条件拒绝，不因预算刚耗尽被立即撤销；sourcebank淘汰时不可再用该源创建局部任务，context改变在缺少有效basis时也先清账。只约束导航，不修改实际标签、计票、mask、恢复或readiness。

S报告87项聚焦测试通过，含真实823单票fixture、第二条路线保留/第三条拒绝、0°及7.9°新源拒绝/9°可、body correction、跨context和刷新stamp负例；独立静态审核未发现阻断项。此前formal18完整反馈事件只记录.094/.032/.031秒暂停，没有>.5秒反馈等待，候选19因此不加入暂停时钟补偿，原7秒路线和90秒总期限不变。本节记录的是实现与聚焦验证，尚未进行候选19实机或正式验收；正式仍0/20。
## 候选19眼形相关核：近边界回原数值路径

四张真实源图的离线CPU profile显示眼形分类是refine主要费用，单纯拟合常量或投影缓存收益较小。候选19仅将原float32眼形模板相关分子的cv2.gemm替换为np.matmul，原模板、81个平移窗口、norm/denominator、whole=False→True顺序、.78分数/.055间隔门及三位置信度规则不变，不调整全局threadpool。全部相关矩阵非有限、分数距.78≤2e-5、top1/top2间隔距.055≤4e-5、未截断置信度距三位舍入半格≤.29×2e-5时，回原cv2.gemm重新计算。guard是保守经验运行机制；160张RGB/347个glyph的最大score误差4.17e-7、决策零变化不构成任意输入的全域误差界或逐位等价证明。

A执行58项聚焦测试通过，覆盖双侧分数/间隔/置信度边界、NaN/Inf、空图、原生与实际warm glyph类别/置信度；独立静态审查未发现阻断项。四个同输入离线clone的semantic wall对照分别为探索513：144.359→86.506ms、探索781：151.967→119.121ms、正式420：198.606→113.738ms、正式911：91.285→70.945ms；原/new observation（排除计时）、pose/refine/association/full result及raw votes递归字段差异均为空，781的24次眼形相关调用中实际有2次guard回原核。运行CV1、实际BLAS16，总过程1.594秒，未调用模型、GPU、采集或输入。

上述clone只以该实际来源已确认glyph子集重建atlas、先前票为空，缺少完整历史状态；离线时钟设为同一保存capture+.1仅用于两候选经过相同年龄门，不能当作真实fresh source证明。字段对照支持这四个离线样本的一致性，速度数字不能宣称实机已经提速，也不能替代完整识别精度验收。证据为`.pytest_tmp/deep_dive_goal_20261002/research/eye_matmul19/comparison.json`；候选19组合联合检查与实机仍待主代理验证，正式仍0/20。
## 候选19联合验证与探索通过

候选19组合联合检查1404项通过、1405项deselected，耗时69.44秒；未将未执行项算作通过。冻结后的探索19实际通过：dispatch 72.625秒、invocation 72.547秒、wrapper 72.047秒、layout core 66.906秒，scan_success/business_ready/targets_readiness=true，43known/六面，U11玩家、D11奇点、F22/B12灵感完整正确。112个实际WGC来源均通过审计，四target均保留同map0、不同真实frame/group的原≥8°SO3见证，最小合格角分别8.5542°/10.6952°/10.1366°/19.1344°，各实体保留正票的最低置信度分别.812/.855/.841/.900。C诊断no_strong_unassociated_current_candidate，没有通过解释添加正票。

局部预算实际summary记录B12/source157、D11/source456、F22/source633各attempts=2，没有靠相同源重复激活获得第三次局部路线。末尾新WGC generation3802的post source age .188秒，完整原生八HUD字段稳定，处理.235秒；harness_errors/failures为空。owned worker及head线程均drained=true/error=null，head owned/in_flight=false，runtime_closed.drain_error=null。113次semantic、491次scene、762次tracking，平均发布源龄.644秒，属于本轮观测；与探索18不同路线，不能把72.625与84.078的差直接全部归因眼形数值核。

探索19证据为`.pytest_tmp/deep_dive_goal_20261002/target_cells_exploration19/live01/round_0001`。独立formal19 ledger的冻结fingerprint与探索19完全相同，原模型SHA256仍为`bff7444233525475ef25173845a6c197c1eec993f2544d204a44846260718123`；开始正式count3时其runs为空、连续计数0/20。探索通过不转入正式计数，不与跨版本结果混计，不称泛化；跨状态操作暂待这三轮正式结果后决定。
## 候选19正式两轮：一次通过后恢复预算耗尽

formal19原count3在第二轮失败后自动停止，只实际执行两轮。两份acceptance记录的冻结fingerprint均与同一formal ledger完全相同；首轮dispatch 73.562秒、invocation 73.484秒、wrapper 73.000秒、layout core 68.672秒，targets_ready且U11/D11/F22/B12全部正确，30known/六面，100个真实来源全部通过审计。四target最小合格SO3见证分别9.1035°/9.0506°/18.9003°/12.9911°，完整原生八HUD字段稳定，failures/harness_errors为空，连续计数实际升为1。

第二轮dispatch 61.859秒、invocation 61.782秒、wrapper 61.610秒、layout core 59.281秒，以anchor_recovery_budget_exhausted提前停止，属于恢复证据失败而非90秒超时。26known/五面，仅U11玩家与F22灵感确认，缺D11奇点/B12灵感；79个实际来源通过审计、这两实体分别有12.6500°/12.8066°独立见证，不替代缺失目标。原生八HUD仍稳定、state_unchanged=true、harness_errors为空，failures仅business_not_ready/entity_set_mismatch；连续计数已重置为0/20，第三轮没有执行。

本campaign结束时owned worker及head线程均drained=true/error=null、head owned/in_flight=false、runtime_closed.drain_error=null。两轮原始证据分别为`.pytest_tmp/deep_dive_goal_20261002/target_cells_formal19/live01/round_0001`与`round_0002`，不得删除第二轮或用探索19通过替代。仍需解决同冻版稳定性，尚未开始跨状态验收或证明泛化。
## 有限目标面页路线提案：等待实现审查

主代理批准独立TargetFacePageScanPolicy实验，旧target_cells保留；拟以六面各两个有限端点、最多一次替代端点依次调度，六页耗尽后回原Cell覆盖。目标候选拟保主面约.88–.92朝向并保留邻面实际known≥2及原多面/总glyph支持；全正对的.99候选不作为默认优选。此为调度提案，尚不能当作已完成的生产实现或实机结论。

页推进必须来自真实不同帧/实际SO3≥8°、精确session/map/frame/generation/time与model coverage等同源证据；body basis变更不能变成新拍摄视角。原Cell的objective_covered及target_indices全complete提前清route可能跳过未兑现端点，需要以独立调度状态处理，不得改真实label/evidence/votes或伪造semantic计数。原冷启动七glyph、已知四glyph共识/多面拟合、弱实体mask、完整quad/HUD/normal corridor和negative证明均保留；两个面页视图只能尝试取得实体pair，不能代替none三票，也不能自动证明灵感清单完整。六页计数不授予99%置信或业务ready，最终仍由原targets_readiness和同源封存决定。原7秒路线、恢复预算与90秒期限不扩大；实际实现和测试将在审核后另记。

## 目标面页路线实现：独立静态审查边界

新增TargetFacePageScanPolicy与公共target_faces入口保留原target_cells供对照，仅用于model目标识别。实际候选主面cos硬范围采用.86–.925、.90优选；主代理明确.88–.92为优先建议而非识别硬门，以保留实际Boss/source543的.874视角与强邻面。规划端点的邻面两glyph是原known_only=False的潜在支持，不冒称已确认图案；实际页来源另要求renewed总支持≥6、至少两面各≥2及本主面≥2，完整model coverage、同源姿态/来源信息、年龄<.8均经过原门。

每页通常两个有限端点，最多一个替代端点；源记录必须严格晚于该端点调度时的实际geometry frame/generation/time水位，同一源不重复使用。两次实际图像的原rotation及body-basis消除后的base_rotation均须相隔≥8°，basis更新仅转换导航goal，不制造新视角。Cell在同一次choose内清route并立即重规划时，cursor于_plan内推进；已有实际source先推进时通过pending cursor防止双进，越界cursor在记录前拒绝。未完成的面只在私有调度complete bitmap中保持pending，原cells/label/evidence/votes不改；六页partial或complete结束后回原Cell，原readiness可提前成功，页数本身不授予业务成功。

该实验有意让真实positive局部任务抢占页面，保留同源局部预算和原时间门；推测assigned hint继续登记、保留原来源证明与12秒TTL，但六页期间不抢占页面，fallback再经父Cell的hint逻辑。hint可能在页面阶段耗尽TTL，是明确行为变化与效率风险，不能宣称旧hint优先顺序不变。独立静态审查已核对source水位、actual/base角度、非交换basis、Cell提前complete与同call重规划边界；实现聚焦/联合结果和实机效果待主代理另记，不运行模型或以静态审查代替正式验收。正式仍0/20。

候选20冻结前，S执行31项聚焦测试通过（1.42秒），根代理另执行3项公共入口接线测试通过，并完成package sync/check/validate；联合检查和实机尚待执行。新增私有hint中断hook在旧TargetFirst默认True，仅新页阶段False，实际新hint不再清route或消耗页端点；真实positive局部任务仍经原父流程抢占。首腿未到共享第二轴轨道而中止时，从当前实际observed重新求pair，保留本页已用attempt与实际source证明，每页最多三次不重置。独立静态复核未发现阻断项。

回归覆盖原Cell.choose同一次调用清route后推进下一端点、三端点有界、旧source水位/7.9°拒绝、非交换basis/context闭锁、旧父hint仍清route/新页不清、partial六页后回Cell，以及实际source845单面B7不能证明多面页。实际seed图姿态配合synthetic AXES生成pair仅验证预测几何，不能宣称实机轴可达或六面一定及时取得；正式仍0/20，原模型、实体正票/negative证明、readiness、来源年龄及90秒门槛不变。

根代理联合回归1430项通过、1405项deselected，74.70秒，exit0；package sync/check/validate均通过，Plan Doctor errors=0、warnings=149（编译缓存）。记录为`.pytest_tmp/deep_dive_goal_20261002/pytest_candidate20.log`及`doctor_candidate20.log`。冻结探索20共1265个代码/运行资源文件，显式`target_faces`、`startup_fast=true`、原FP32模型；首轮实机进行中，探索通过不转为正式连续计数。

## 候选20首轮实机：有限面页未能补齐D/B

冻结后的探索20失败：dispatch与记录invocation均90.329秒、wrapper89.734秒、扫描core85.031秒，time_budget_exhausted/partial，34known/五个observed面。仅U11玩家/F22灵感确认，缺D11奇点/B12灵感；两处保留的原actual SO3独立见证最小角17.2815°/18.7948°，129个实际WGC来源全部通过source审计、同map0，不能据来源合格声称目标集完整。D11最终unknown只保留frame489的singularity/.770一票，B12没有保留实体正票，未见已经合格的D/B pair被导出遗漏。

实际page order为U/R/F/D/L/B：U无支持pair零attempt；R两attempt后无支持pair；F两attempt并取得实际source340与371（cos .873844/.900652、支持R3/F4与R4/F6）；D三attempt只取得source472（cos .872039、F7/D3）；L一attempt后无支持pair；尾部B仍active、attempts3、accepted_views0，fallback=false。只F页complete，其他partial保持导航失败记录。两次面页来源不等同于两实体票，尝试次数也没有写入语义计票；有限路线未保证在90秒内取得所有目标，本轮不能算方案达标。

完整failures为business_not_ready、state_change_unresolved、over_limit_dispatch_wall_sec、over_limit_invocation_elapsed_sec、entity_set_mismatch。post_hud_attempts为空，harness报告post_hud_dispatch_budget_exhausted/public_dispatch_budget_exceeded/task_not_terminal_success；state_unchanged=false表示尾部没有预算完成post证明，不是实际观察到HUD改变。151次semantic、691次scene、1010次tracking，平均semantic发布源龄.622秒；这些是不同导航路线的本轮观测，不能直接作为同路A/B提速证据。

runtime_closed记录owned模型worker与head线程均drained=true/error=null，head关闭后owned/in_flight=false、drain_error=null，156次实际模型执行/3次缓存命中。原FP32模型SHA256仍为`bff7444233525475ef25173845a6c197c1eec993f2544d204a44846260718123`。原始证据为`.pytest_tmp/deep_dive_goal_20261002/target_faces_exploration20/live01/round_0001`；本轮属于探索，正式仍0/20，不改写此前失败或开始跨状态验收。

## 候选20的D控制证据：端点已到，不把静止图重复计票

只读对齐保存actions.feedback中的82个D input-policy记录：首次D/rev5被F22真实positive局部任务rev6抢占，之后rev7仍为D cursor0、attempts2，frame428至472目标角20.168°降至最低3.553°；rev8为cursor1、attempts3，frame475至487目标角17.879°降至3.112°，实际控制方向近水平。后续Boss源489/493/499/504/511的保存姿态接近该已到端点，相互最大间隔仅约1.654°；这是到达后的狭窄视角cluster，不能产生第二个原8°实体见证，也不支持“轴逆投影根本到不了该端点”的解释。

保存input-policy在frame491已切至L/rev9，D记finite_endpoints_exhausted，后续D语义源仍在陆续处理；例如489记录glyph age .938秒，而493/499/504/511记录.406/.563/.641/.594秒。捕获帧与发布/控制消费不是同一时刻，不能从已保存semantic图就假设页策略已经及时收到它；现存input记录也不是逐tick发送ack。D页的几何source472（以及其与484等后续姿态的独立性）不是两次Boss positive：实际Boss高置信只在到达后一处cluster出现，最终仍只有一票。本轮没有证明需要改输入轴、恢复timer或年龄门；优先减少规划阻塞，再核验实际semantic消费与有限补视角。

## 候选21：保持候选与评分的面页求解优化

生产唯一改动为_select_page_pair内等价剪枝：两个轴的132个固定角度矩阵仅在本次调用复用，不跨输入或校准轴缓存；原1530个pose、candidate id、path、cost、family顺序保持。先用原exact cos范围排除候选，再用原readability/邻面两潜在glyph/主面六格门筛选，最后只为尚合法的端点计算原planning support与route support；原本invalid的数组位置填0且不能进入choices。原family/tie、utility、第三端点和body-basis转换保持，调度、票、readiness及所有来源/年龄/90秒门不变。

独立静态审查确认readability/support按每pose计算，没有跨候选的统计依赖；route minimum仅依赖该原path，因此不合法候选的剔除不改变保留候选的支持值。S执行35项聚焦测试通过（4.20秒），其中unknown、known sprites、非交换basis、无pair四个固定输入与冻结候选20原函数的selected pair/path逐元素array_equal，rows不变。该对照仅验证四个离线输入与剪枝结构，不是实际历史全量重放、实机速度或99%准确率证明。

根代理联合1434项通过、1405项deselected，72.64秒、exit0；冻结1265个代码/资源文件，显式target_faces/startup_fast=true、原FP32模型。候选21单轮实机已开始，效果待其原始记录审核；本节不提前写通过，探索不转入正式计数，正式仍0/20。

## 候选21实机：规划费用下降，恢复仍提前失败

探索21实际dispatch51.985秒、invocation51.891秒、wrapper51.375秒、core47.531秒，以anchor_recovery_failed/blocked提前结束，未触90秒超时。28known/四个observed面，仅U11玩家/F22灵感确认，D11/B12缺失且最终均无保留实体正票。55个实际WGC来源全部通过source审计、同map0；玩家frame97/generation852与116/941的最小合格实际SO3角14.829819°，F22 frame308/1648与408/2031为9.964359°，两处正确仍不能替代完整目标集。

从所有保存actions及input反馈policy中，按每个plan_revision仅取第一次出现的planning_time_ms求和：探索20的12个revision累计17.412741秒，探索21的10个revision累计.915644秒。这是两轮实际保存的规划计时口径，不能把不同路线、不同调用数量及不同图案/遮挡状态的差直接当作同输入因果A/B，也不能宣称整轮节省16.497秒或可靠性已解决。selector同输入逐元素对照与本轮计时共同支持继续消除计算浪费，实机业务失败仍完整保留。

本轮page尾部B active、attempts1、accepted_views0；U/R/F/D/L均partial，fallback=false，不能用预测六面路线代替实际source证明。末尾执行一次新的完整native post-HUD采样，处理.250秒，eight_hud_fields_stable/state_unchanged=true、harness_errors为空；failures仅business_not_ready与entity_set_mismatch，属于业务识别失败而非HUD审计误拒。

原模型执行77次、缓存命中2次，模型SHA256不变。runtime_closed记录worker与head线程均drained=true/error=null、head关闭后owned/in_flight=false、drain_error=null。证据为`.pytest_tmp/deep_dive_goal_20261002/target_faces_exploration21/live01/round_0001`。本轮探索未通过，正式仍0/20；继续审查页面与真实局部任务结束后的路线状态，未调整恢复、计票或来源年龄门。

## 候选22实施与审查中：真实源约束的目标面构图

新增纯导航helper冻结assigned interest首次登记时的原始atomic semantic packet与目标quad。R/tvec来自该源pose，按原几何直接投影，不能借当前controller pose或后来更好的packet补造历史ROI；来源frame/session/map/time/body basis、原model coverage/renewal、该面glyph与原association certificate、box/point绑定。坏creation source明确拒绝并保存有界诊断。helper保留原interest的expiry与当前authoritative attempt计数，不能重启12秒TTL或回退原预算；这里只消费导航证明，不生成正票。

所有页的原有限eligible pair先按union9是否成立、实际预测可读格数、原utility排序；有该面有效冻结hint时，两个主端点还必须令每个目标原readability>0，私有planning rows使用原源bbox/quad预测有限高度遮挡。union9和target readability都是导航预测，不证明实际图像完整或99%识别精度。新hint只排队，在原有限boundary从actual observed重解，不清正在执行的route、不重开每面三次attempt；实际端点消费原hint attempt最多两次。原真正positive局部任务及恢复进入时置resume标记，父流程提前清task后仍从实际返回姿态重新求page，保留已用attempt与真实source证明。

A执行helper35项聚焦通过（.24秒、进程1.86秒），S连接联合窄测80项通过（4.47秒），新增有界诊断等11项通过（.34秒）。保存20源484/963和21源476的格式冻结均有效，仅使用保存来源与原source-basis转换作离线合同审核，未做新模型推理/拟合或宣称真实fresh。独立静态审查其余source binding、budget、private rows、natural boundary与resume顺序未发现阻断，原source水位、actual/base双8°、拟合、计票、readiness、恢复及.25/.6 dwell门保持。

主代理审查另发现两项待修阻断：最多一个第三alternative原只来自几何eligible集合，仍需保证所有active原hint目标在此端点readability>0；framing=False离线21对照的boundary hint集合比较应显式关闭。联合测试运行期间保持源码冻结，待结束再做窄修及回归，本节不签实机ready、不提前写联合或实机通过。捕获afterarrival的dwell提案未批准：原revision变化可能消费arrival前RGB，但21平均发布源龄.668秒已超过原.6秒上限，尚不改变时间或门。正式仍0/20。

候选23仅有隔离研究文件`research/local_positive23/{prototype.py,test_prototype.py,README.md}`（在repo的`.pytest_tmp/deep_dive_goal_20261002`内）：研究已由原known-four共识认可且有严格同源positive的private local-only source，不进入共享page/hint/recovery/readiness源库。actual687 loader使用原保存publication与原retained positive，不能合成新的fresh时钟；保存HUD是原native passed guard摘要，不能称八字段独立OCR重验证。该原型仅写作，测试尚未执行、未接生产或实机，不改变候选22或正式0/20结论。

根代理完整22联合回归已完成：1480项通过、1405项deselected，73.54秒、exit0，日志为`.pytest_tmp/deep_dive_goal_20261002/pytest_candidate22.log`。这份结果发生在第三alternative/False boundary两项小修之前，不能称为修复后的完整联合验证。已有package checks通过，未改变decorated metadata；小修冻结后还需针对两个已知阻断的聚焦覆盖及独立静态复审，实机尚未批准。

两项小修现已冻结：framing=True时第三alternative仍须满足原角度/family/geometry集合，并额外要求全部active冻结目标原readability>0；若没有合格第三则仅保留两个主点，不以遮挡目标的端点补次数。framing=False明确不触发hint-set boundary重解，保留旧21 kernel对照。S执行修后13项integration通过（.44秒），覆盖两个主点可读但第三点目标不可读的拒绝，以及False boundary不重选；独立静态复审未发现剩余阻断。上述1480完整联合仍是小修前结果，实机效果待根代理审批及原始证据，正式0/20。

根代理随后批准候选22冻结及单轮实机，独占CPU/GPU/WGC/输入；其实际结果尚待保存证据审核，不提前称通过。候选23研究原型继续只写未执行，正式仍0/20。

## 候选22首个campaign：初始HUD证明未通过，尚未扫描

`target_faces_exploration22/live01/round_0001`实际dispatch/invocation5.062秒、wrapper4.485秒，以initial_full_hud_not_complete_stable退出；没有scan/core结果、实体来源为空、model_executions=0。因此本轮没有检验新构图路线的效果。首个失败campaign及原ledger attempt保留，不因后续同冻版重试删除、改成通过或计入正式成功；正式仍0/20。

两张实际WGC pre来源均在同一session，generation117→201、frame_time40923.843→40926.312严格前进。已读八数值相同：plane1、rounds6、moves0/1、rotations1/1、灵感0/2，均为有效board/player_turn。pre1 source_age2.438秒超过原.5秒门，pre2为.172秒。另一个重要边界是pre1并非八字段全native：moves数字0原native confidence .7185、margin .0546，记录ambiguous_numeric_glyph，随后moves_used/total使用ocr_pair；pre2 moves native confidence .9182，全部八字段native、无OCR请求。现存记录没有分解native冷缓存、OCR和其他处理的耗时，不能把整段2.438秒断言为native冷启动，也不能把这次失败简化成仅首源过期。

本轮state_change_unresolved/state_unchanged=false表示初始fresh证明未建立，不是已观察到数值改变或页面变化。owned worker关闭drained=true/error=null，head未启动、owned/in_flight=false，drain_error=null。根代理随后同冻版启动live02观察是否重现；门槛、源码、90秒时钟未改变，本节不提前写第二轮结果。

窄解决方向仅为待审批理论：初始完整native读取保留原组件/置信门，遇到unknown或处理后过期时，在原dispatch+90秒硬截止内有限获取新WGC源，每次PNG/JSON/hash/字段来源单独保留，最终须两张独立新源各自八字段native完整、fresh且稳定。所有尝试中已实际读出的字段一旦相互矛盾立即失败，不能挑后续恢复相同的画面掩盖变化；不能拼接两帧的已知字段，也不能重基时钟或延长预算。模板预热可作为另报成本的环境准备，但无法修复此次真实字形歧义，不能代替新帧证明。本节未实施retry或修改现有初始门。

## 候选22第二次：完整HUD稳定，识别恢复失败

同冻版`target_faces_exploration22/live02/round_0002`实际dispatch79.750秒、invocation79.672秒、wrapper79.172秒、core75.094秒，以anchor_recovery_failed提前失败。U11玩家、D11奇点、B12灵感正确，F22缺失，目标集不完整；原实际source审计中三实体分别有≥8°正票对，但不能替代缺失F22。八字段post-HUD为eight_hud_fields_stable/state_unchanged=true，harness_errors为空，失败项仅business_not_ready/entity_set_mismatch。第一campaign初读失败仍保留，第二次也未成功，正式仍0/20。

本轮模型执行115次、cache命中2次；实际`live02/runtime_closed.json`记录worker_close与head_thread.close均drained=true/error=null，关闭后head owned/in_flight=false、drain_error=null。没有把初读重试或23私有源原型接入本轮。

主代理随后批准23隔离原型CPU测试，12项通过（1.51秒），使用actual19formal round2/source687的真实publication及原positive，没有重新拟合/模型推理/游戏输入。首次loader因frame无顶层map_revision失败，修正为读取实际semantic metadata并核对frame.pose.map_revision后通过。原型仅证明private-only predicate和自身预算；目前没有跨global/private共享原始source计账，不能证明库切换不重开两次/六秒预算或跨库8°历史，因此尚不能生产集成。该缺口已记录研究README，正式仍0/20。

## 已批准的初始HUD有限新源证明修复

证明口径保留原逻辑：两张完整baseline各自在读取、保存、核验时源龄不超过.5秒；第二张返回时第一张可能已超过.5秒，不能声称两张在core开始时同时fresh。初始化先建立DML session，但初读通过前不执行模型推理。

仅live acceptance harness初读改为native_only，每轮最多四张独立新WGC源，在原dispatch+90秒硬截止内获取、读取及保存。每张PNG、JSON、hash、RGB digest、实际session/generation/frame_time和八字段来源独立保留；unknown或处理后source过期只能重新获取下一张，重置连续完整计数。最终连续两张fresh≤.5秒、八字段native完整且稳定的整帧才成为post-HUD baseline，部分样本永不拼接字段或进入baseline。所有尝试中任意已实际读出的同字段整数矛盾立即失败；非native、跨source、页面异常或raw证明不一致立即闭锁，不挑后续恢复相同的画面。原post-HUD、业务source/拟合/投票/readiness门及硬90秒保持。

新增initial_hud_attempts/reason/elapsed审计进入原harness保存与acceptance_record；失败attempt不会删除。授权CPU聚焦114项通过（4.23秒），覆盖partial→两个complete、stale→两个complete、中途partial重置、字段变化立即停、四次耗尽、预算不足不捕获、处理/保存越截止失败、实际来源/PNG/JSON/hash变化闭锁，以及原post-HUD和harness回归。没有执行模型/GPU/游戏输入；这些是构造/离线合同测试，新初读行为尚未实机验证，23局部源研究仍未生产接入，正式仍0/20。

## 候选23：保留邻面支撑的有限角度实验

22第二轮原图显示F22灵感曾露出，但F面本体始终处于极侧缘或HUD下方。700/707/713的检测与nominal F26关系不能替代own-face图案证据，原拒票保留。F页面没有合法端点对，末段D5单面图案也不能替代多面恢复证明。视觉审计见research/candidate22_round2_visual.md；它没有执行模型或重新拟合。

以真实保存560packet及实际校准axes、全unknown rows计算的有限下界proxy中，原cos .86–.925没有合法pair，.78–.94和.75–.95均找到union9 pair。560并非当时562规划geometry，当时atlas未保存，所以不是精确历史重放、当前fresh证据或实机效果。根批准候选23只将页面导航范围改为.78–.94、导航actual-view最低cos同步.78；原实际拟合、多面源、独立8度、positive/votes/readiness不变，同时包含已测试的native初读修复。局部known-four positive研究不接入这一版；dwell、恢复、路径支持和总90秒不改。待源码冻结及聚焦回归后单轮实机，正式仍0/20。

## 候选23实际探索通过，正式第一轮恢复失败

候选23页面cos私有范围校验有限0<min<max≤1，默认.78–.94；有限候选与actual page最低cos读取同一字段，summary记录实际范围。原多面/8°来源证明与实体识别门不变。S聚焦52项通过（4.22秒），旧kernel对照显式保留旧.86–.925范围；这只验证导航候选合同，不能称新增角度都能达到或都会产生可用实体证据。

`target_faces_exploration23/live01/round_0001`实际dispatch74.657秒、invocation74.563秒、wrapper73.969秒、core68.656秒，业务targets ready，41known/六面，U11/D11/F22/B12与独立人工真值一致。保存99个审计来源均实际WGC、map0；四目标各有不同frame/generation及原≥8°正票对。最小合格对分别为玩家168→231、9.306462°；奇点717→735、8.481915°；F灵感461→485、9.326663°；B灵感726→800、19.386400°。这些是本轮actual association rotations的实体证据，不把页几何pair或预测端点当成正票。

本轮初读实际两张完整native源通过，generation125→136、frame_time42349.234→42349.437，八字段均原native来源、无OCR请求，处理后source_age .156/.141秒、初读总.406秒；没有触发第三/第四次重试，因此不声称实机已验证partial retry分支。post-HUD八字段稳定，harness_errors为空。模型执行113次/cache2；worker与head均drained=true/error=null，关闭后head owned/in_flight=false、drain_error=null。这是同一人工核验棋盘的一次探索通过，不加入正式计数，也不证明跨状态或99%泛化。

根代理随后同冻版启动正式count3。`target_faces_formal23/live01/round_0001`实际dispatch43.016秒、invocation42.953秒、wrapper42.438秒、core38.844秒，以anchor_recovery_failed提前失败，24known/三面，仅U11/F22正确、D11/B12缺失。初读两张native完整、source_age .125/.109秒，post-HUD八字段稳定、harness_errors为空；失败项仅business_not_ready/entity_set_mismatch。模型执行67次/cache2，两线程实际drained=true/error=null、drain_error=null。正式失败record完整保留，后两轮自动停止，正式仍0/20；不得合并本节探索成功与正式失败为稳定通过。

跨状态oracle暂不执行，先解决本次恢复稳定性。未来以当前人工真值和实际HUD为起点，moves0/1、rotations1/1表示尚有一次合法移动、已无本回合旋转额度，不能为制造新状态再猜一次layer旋转。可在人工确认目标面/相邻空格及游戏合法操作后，仅移动玩家一格（例如直接确认U01为空且可达后U11→U01），保存动作前后原图、操作与HUD账；已有流程将该动作视为本回合最后玩家操作，可能出现节点事件或enemy turn，不能假定其他实体位置保持旧值，更不能宣称此操作可立即逆转。等待游戏进入下一稳定player board后，人工逐面核对全部目标、面名/3×3位置及HUD，保留实际原图和独立标注，重新登记独立truth ID；不能使用被测识别器输出作为真值或用旧位置变换替代核验。保持冻结代码/模型/配置，同fingerprint不同truth可续正式streak，所有失败照计；灵感数量由每轮实际HUD total-collected得出，无需为合法数量变化修改冻结配置。

## 候选24：真实端点语义反馈闭环

正式23首轮43.016秒/core38.844秒失败，U/F正确、D/B缺失，后两次自动停止，正式0/20。实际F路线332/333几何源已到端点约3.7度；迟发布的328语义源却在到达前拍摄、距目标7.041度，任意revision变化触发下一路线339。真正端点后的338语义源稍后才发布，已经早于新dispatch水线，被正确排除。这个实际链路支持修复端点反馈，不能把所有R页失败或全部恢复失败都归因同一时序。

Cell增加protected waypoint反馈hook，默认仍原revision变化和到达后.6秒；仅页面末端锁定首次真实tracked arrival完整frame/generation/time水线和elapsed起点，合格语义必须捕获严格晚于该水线且满足原model/fullcoverage/fresh/至少6点多面/own-face/pose/8度要求。页面截止固定arrival+.9秒，poll或姿态抖动不会续期；超时新源也不得兑现页面。原在6度范围内提前获得的合格真实页面来源保留，local/recovery/context/newdispatch按原有限预算处理。实际目标票据、readiness、拟合和整轮90秒不改。

最终根代理联合197项通过（9.94秒），包括真实choose链、迟发布预到达源、绝对截止、旧.6行为、页面/初读/恢复回归；不是实机验证。初始63项结果在补充真实controller和late-deadline测试之前，最终15项窄测试与197项联合结果属于最终冻结版本。待单轮实机，不提前计入正式。

输入响应只读审查另确认continuous两轴在初始化后冻结，observed_motion_rate只改规划成本，未更新像素灵敏度；斜向_learning数学缺陷没有在实际23 continuous发生。后续需真实input时间账和去除body校正的base rotation才能审查更新灵敏度方案，目前不与24混改。审查报告research/input_response23/README.md。

## 候选24实际失败及候选25输入记录对照

24探索实际89.922秒/core84.937秒耗尽时间，U/D/B正确、F缺失，34known/5faces；post-HUD因90秒预算耗尽未完成，state_change_unresolved表示未知，不能声称游戏真实改变。两识别owner均drained/error=null。F3attempt/0view，D0attempt/no-pair；实际全部保存geometry的F中心cos最大约.550。日志中最小2.5度是中间waypoint，源627/671/679距离最终F目标仍88度左右，750仍61.765度；随后真Boss局部确认打断路线。F端点arrival始终None，所以本轮不能归因.9反馈闭环失败或仅延迟。停止昂贵whole9远端Euler页面路线，保留失败和修复源码，正式仍0/20。

另保存stage统计：source489 total4.0618秒/refine1.6063/atlas2.2478；551 total3.044/refine2.031/atlas.842。两源均13candidate/8confirmed/seven_centres(R9D4)，不能把峰值猜成known-four组合枚举或神经检测。另行准备分段profile，不放宽原判定门。

候选25改用原target_cells短程覆盖作为对照，原local-source两次预算、真实positive实体footprint、matmul优化及native初读保留；不增加实时gain、不延长原Cell .6或90秒。新增InputJournal纯诊断：完成native输入前/后时间、真实整数dxdy/累积量/grip与源身份，每个新geometry原R/basis及去除虚拟校正的base_R。预测512deque不变；记录每channel最多16000，truncation显式，invalid原始源/矩阵不会成为有效物理姿态，cancel记录保留。normal和取消drain后写入诊断，scan_started_at绑定已有grip秒数。日志没有fresh/vote/support/readiness权限。23 helper聚焦通过（.27秒）；根最终149项联合通过（5.73秒），包含native输入总量、语义更新不复制同geometry、取消落盘、旧短程策略、初读及owner drain。实机结果待测。

## 候选25实际探索和正式三轮（2026-10-03凌晨）

同一FP32/DML冻结配置改用target_cells。探索dispatch57.657秒/core52.125秒，U11、D11、F22、B12均与独立人工真值一致，36known/六面，83模型执行，八HUD稳定，owner全部drained。InputJournal记录825次实际输入/547个geometry，全部合法且无截断；124个单轴同向段中没有符合前后静止和时序条件的有效响应窗口，因此没有拟合或更新灵敏度。36个body correction事件需去除虚拟校正，raw/base角增量差中位.552度、最大2.665度。不能用这些输入段声称已经测出实时gain。

随后同冻版正式count3：round_0001 dispatch70.375秒/core63.797秒、round_0002 dispatch69.672秒/core65.984秒，四目标全正确且八HUD稳定；round_0003 dispatch91.781秒/core87.781秒，time_budget_exhausted，U11/F22/B12正确但D11缺失。第三轮post-HUD预算耗尽，state_change_unresolved表示未知，不是证实游戏状态改变。失败项同时包括业务未完成、目标集不匹配和总耗时超过90秒。runtime_closed中drain_error=null，模型worker与head均drained。三轮记录完整保留，正式连续计数重新为0/20，不能将前两轮与探索成功合并为达标。当前停止盲目重复，分别审核失败轮路径、视觉正证据和输入/结束清理边界；尚未改变识别门或更新灵敏度。

## 候选26最小导出改进与短程实体取景原型（准备中）

formal25r3输入源发布延迟与前两轮接近，没有持续native输入阻塞；r3停止拖动的公共elapsed87.078秒，core result stamp88.375秒，core返回91.688秒，观察终态91.781秒。最后release后完整收尾4.61秒超过原3秒reserve。不能把drain/report排除总时间。只读视觉/路线审计确认r3 D最佳源413/417，cos约.803/.796，D11quad下界602/606且浮起Boss处于Reset/底HUD；只有弱框.284/.290、无强框/正关联/票。成功前两轮D最佳cos约.889/.891、quad下界549/537，实际强Boss框.777/.864。r3并非never-frontal或known-four恢复失败，128/133实际renew；D只有29.281–38.515秒曝光窗口，少量角格none让后续路线转B，晚D再访未到。保留所有原阈值。

批准离线原函数导出单轮：crop .93183秒、report1.11811秒，46次PNG解码/32原源，重复14次解码合计.250716秒。原JSON/PNG只读、SHA未变，非实机因果AB。根实现8源有界LRU保持cell遍历/错误顺序、所有字段完整的compact JSON（layout/feedback/drained）、summary导出计时；ScanVisionStream.stop新增join/merge/write计时，不更改排空或取消顺序。预算仍90，未增加reserve来掩盖耗时。

同时批准独立target_framed实验策略：从实际姿态执行≤30度短程候选，以未知目标完整height-band/billboard在安全区域的新增机会优先，原parent local/source总账/正票门保持；实际model/fullcoverage/fresh多面source和实际mask才可记导航机会。每面有限尝试，无路线或无新增机会退让原Cell全局覆盖，不强制54bit/9整面才结束，不写标签/票据。尚未完成源码/回归/冻结或实机，不加入正式计数。

导出合同6项通过，modified offline输出与原46裁剪PNG逐字节一致、完整JSON对象一致、原源SHA一致。解码46→35（8源LRU合法驱逐后仍3次重解）、JSON13,106,748→5,719,139字节，HTML未改。该单轮modified crop1.241/report1.648秒比原更慢，不声称加速；只有重复工作/体积下降已验证，实机等待分段计时。

根另按实际4.61秒收尾加post-HUD需求，将acquisition reserve从3调整为6秒，在同一原请求预算内提前停止采集，公共90秒仍完整计drain/export/HUD；这不会延长识别或把未识别目标计为成功。该保守预留不能代替路线改善或证明所有OS调度都不超时，所有超过90秒的实际记录仍失败。后续实机必须审查stop_timing/export_timing和完整dispatch。

联合回归（最后world-envelope/mask修正前的冻结源）1580项通过，87.99秒，log pytest_candidate26.log。package check/validate通过；doctor errors0/warnings152/checked139，相比候选20多3项warnings分别是input_journal/target_framing/target_framed生成pyc，其余全部既有pyc/cache类型，没有删除。独立静态审查另发现未知实体radius .6tile=.516 world、高度1tile=.86 world小于实际已观察Boss框及既有physical走廊上限1.0；actualmask只billboard可能漏quad/corridor角落。根批准以显式world先验height0..1.0/radius至少1.0修正，并保留实际quad/corridor±14/billboard三者OR mask检查，无owner豁免；待最终聚焦回归与复审，不把1580旧结果冒充这两修之后的回归。

两项world-envelope/mask修正25测试通过后，独立复审发现新增默认(3,1)tvec广播运行错误；根局部reshape(3)修复并补默认column、explicitvector同值、36候选真实函数调用与状态恢复测试。最终联合187项通过8.68秒（final2），此前186/1580不能覆盖这个最后修。CLI manifest check/validate正常、doctor0errors/152cachewarnings如前。未知先验height0..1world/radius1包住本批6strong bbox+8，但6个strong源都被Reset/安全区拒；仅是保守导航假设，不是最大实体理论覆盖界，保留无safe时fallback。

target_framed_exploration26 实际dispatch65.672/core60.391秒，U11/D11/F22/B12全部正确，35known/六面，model99/cache2，native初读.438秒/post8HUD stable。Stop分段总.235151秒（geometryjoin.007392/scenejoin.080296/semanticjoin.0000094/merge-final.037763/writerjoin.109544）；没有据不同路线单轮比较宣称export因果提速。新导航bitmap仅9/54（U1/F2/L6，D0），尝试U2/F2/D2/L2，仍通过原实体正证据闭合而非强制全54。新route确实可退父覆盖，不能声称所有脸完整安全覆盖或证明新策略比旧版稳定。Runtime_closed error=null，worker/head drained均true。探索不计正式，正式仍0/20；根继续同冻版count3。

同冻版target_framed_formal26 count3：r1 dispatch60.547秒，四目标正确、8HUD稳定；r2 dispatch40.625/core38.062秒 anchor_recovery_failed，仅U11/F22，D11/B12缺失，25known/四面。r2新bitmap3（U/F/L各1），U2/L2尝试，其余0；stage stop1.704445秒，主要semantic join1.231841秒。HUD八字段稳定且harness errors为空，runtime/head/worker全部drained/error=null，确为业务识别失败而非状态未知或超时。默认首败停止r3，连续正式重新归零。根停止重复并授权两个只读实证审计（route/recovery 与视觉/拟合，累计CPU各≤15秒，无新推理/live/源改动）。目前该策略未满足稳定要求。

正常GUI与自动planned_run仍是cells/startup_fast=False/defaultbudget60，与harness explicit target_framed/True/budget90和隔离DML不等价。caller边界研究candidate26_caller_boundary.md仅静态。独立scan成功不能宣称scan→strict registration→supplement→planning→actualoperation闭环已验证；未来经过稳定验收后应单独接入并验证，不能测试完又改source再沿用旧fingerprint计20。

## 恢复后候选26失败实证与候选27准备

最新继续任务后重新读取实际日志和游戏画面。formal26第二轮不是短程L路线仍在执行，而是父全局D路线在已知F/L接缝丢失L支撑：source308的实际接受F8/L6，首D路标对其真实已知格的投影只有F7/L0。恢复目标经body basis转换确为source308，实际到3.56度内，及时source367仍只有F8/D1；不是一直到不了目标。404最终获得F6/L5且残差合格，但在stop后完成，不能算成功；其源龄1.516秒也不能用完成时间改写为新鲜。所有45源模型实际执行/覆盖有效，Boss提案0，未获得合格D11取景。连续正式仍0/20。

只读审计报告已复制到deep-dive-recognition-20261002：formal26_round2_route.md、candidate26_formal2_visual.md、candidate26_source404_cost_paths.md。源404两次bounded fit为13点筛11点后的合法重拟合，不能删除；同帧目标关联已有缓存。窄优化候选为refine完成后的相同R/T/源下visible投影复用，不跨图/姿态/身份，不删完整54竞争证书。代理正在实现和验证；目前不声称速度提升。

根增加纯诊断accepted_cell_indices、accepted_confirmed_cell_indices，来自每个成功refine分支真正使用的support/inlier集合，stream深拷贝进同源accepted_anchor_observation；candidate或整face计数不能冒充实际坐标。43聚焦测试通过1.17秒，Manifest check正常。候选27实验策略将使用这些实际源格检查完整短路线及父fallback，原像素/置信度/残差/新鲜度门不变。已合法旧源只作导航历史，不能授予新正票或续新鲜度。尚未联合冻结/实机；下一轮失败不得混入候选26计数。

实际候选26导出计时已正确读取public user_data.deep_dive_scan，explore/r1/r2总export分别1.770772/1.416188/1.603813秒，主要HTML构建.778588/.539355/.712989秒；不与不同条件的旧版单轮作因果提速比较。

## 候选27探索失败：完成条件与取景目标冲突

全相关1617单测通过91.20秒，Manifest check/validate正常，冻结1268源文件。探索27实际dispatch12.500/core9.204秒，joint_navigation_no_supported_route，只有U11且不具两个独立正证据组；业务未就绪，记录失败。HUD八字段stable，ownedruntime/head/worker排空正常。该轮不计正式，正式仍0/20。

首失败与初始源资格缺失无关：实际source17 model/fullcoverage有效、age.593秒，accepted U7/F5十二格，实际mask/readability过滤后U6/F3九格，最新mask没有再次遮挡这九格。路线只发送约6px，U2/F1取景预算很快消耗；父Cell.choose把ordinary none已确认视作目标完成并立即清route，但该策略的完整浮起目标取景bitmap尚未完成。代理的离线stop-geometry proxy仍有16条source-supported短候选，6条有取景gain；parent未知面过滤只选D/L/B时则没有positive safe路线。不能把这个proxy当真实新版执行。

批准候选28最小完成hook：原父路线仍按complete目标结束；仅ownedframed路线依实际framed_seen或原已证positive结束，ordinary none不能提前清路线。不更改_objective、标签/投票/ready、实际源门或预算。根新增base _route_targets_complete默认原allcomplete调用，默认/targetfirst/mixed/anchor/incoming/recovery118测试通过4.35秒；首命令文件名错误未运行测试，后正确路径重跑，失败log保留。代理owned override/实际choose回归待联合冻结，再实机，不提前声称解决后续三面交接。

候选28联合232测试通过9.09秒，连同默认118回归覆盖base completion hook。实机探索28 dispatch16.203/core12.625秒仍joint_navigation_no_supported_route；只有U11，但这轮两个独立正证据组验证通过，harness_errors为空。HUD八字段stable，ownedruntime/drain正常。连续仍0/20，未建立正式28campaign。

实际continuous输入258.522px（另有原校准两150px），已明显推进而非候选27仅6px。新source61真接受U8/R6/F6二十格，原mask/readability过滤后U4/R2/F3九格，第三面R为实际新接受格，不是potential；私有bitmap仍U1、U尝试2。继续只读审查后续桥接：当前九支撑格释放U只剩F3/R2=5，不能把新D潜在预测当第六格。可能需要一次source-preserving短过渡，在新面尚无三格gain时提高它的正面度，以真实下一图刷新支撑；有限尝试/不续旧clock/不写票。该桥接目前仅理论，未批准实现或实测。

## 候选29、30实际结果：桥接历史与活动路线复核

候选29增加有界导航桥：仅普通正收益路线不存在时，使用原36短候选、原6格/两个面各2格实际支撑，改善一个弱面正面度。每面两次封账、下一次必须有新源及8度独立姿态；最终实际追踪到达和真实源距目标≤4度、原0.6秒端点等待均保留。不写标签、票据或framed bitmap。61项聚焦及247项联合测试通过；冻结实机dispatch38.156/core34.0秒，joint_bridge_no_new_source，U11/F22正确，D11/B12缺失。HUD稳定、owned runtime全部排空，探索失败不计正式。

第二个桥错误回到历史已经安全支撑的U面，因为当前源只见R/F而将U0当作未建立的新面。晚源394仍距桥目标约4.34–4.41度（控制/融合姿态口径不同），原4度门正确拒绝；下一源402融合姿态距目标2.267度，但观测鲜度检查已经晚于最后控制停止约0.125秒，不能算live已消费。该轮没有Boss提案，不放宽姿态或等待门。证据分别见candidate29_audit_and30_history.md、candidate29_bridge_visual.md。

候选30只增加同session/map的ever_supported导航历史：原实际接受indices经真实遮挡/可读过滤形成合格联合源后，该面至少2格才登记；bridge不再选历史已经安全建立的面。basis暂失保留历史/尝试封账，禁止当前动作；真context变化清除。64项聚焦和250项联合测试通过10.75秒。实机dispatch13.781/core10.313秒，joint_navigation_published_route_unsupported；仅U11且独立实际正证据对未闭合，HUD稳定、owned runtime排空正常。此失败发生在bridge之前，不能据此判定history修复无效；正在核对旧活动路线在后续实际源/遮挡/姿态更新后的安全性。正式连续仍0/20，同一棋盘探索不能充当跨状态99%验收。

候选31失效旧route允许一次同choose当前证据重规划，原门再验；不重置任何尝试/源clock，不加等待。68项代理聚焦后，根将safe replacement从零位移observe改成真实36候选中支撑合格且>4度的目标，验证第二次规划确可安全发布方向；最终联合254项通过11.55秒。实机dispatch25.828/core19.016秒，joint_bridge_no_new_source；正确U11且独立正证据对通过，另外三个目标缺失，HUD稳定、ownedruntime/head/worker全部排空。历史U/R/F真实支持均登记，bridge实际选D而非回U，D预测cos-.50966仍隐藏；没有正式连续计数。末及时语义源135真续锚U4/F4，processing.734、publish latency.922超过原.8秒导航源资格门，因此joint仍保留117。正在分辨实际到位、源姿态和处理延迟，不据此放宽门槛或延长等待。


候选32仅真实Cell最终semantic_dwell_timeout已消费、当前实际joint仍安全时允许有限再规划，不追认过期桥ready。74聚焦后联合260项通过9.59秒。实机dispatch26.609/core22.907秒，joint_bridge_endpoint_not_reached，仅U11但独立正证据合格，HUD稳定、ownedruntime全部排空。saved summary证实joint_route_replans2、bridge_timeout_fallbacks1，D/B/L各桥一次；末L为8+22度两段路线，未最终到达时出现F22实际强提案及source227 assigned interest。正在审查新兴趣清route与pending桥阻断是否冲突，尚未将中断计为成功或修改规则。正式连续仍0/20。

32根因后续审计：L首/最终路标距stop242分别8.0493/23.7046度，建路线后只过1.141秒，未到7秒approach或0.6秒dwell超时。source227原严证书创建F22导航interest（不是已确认positive；bbox592..643受HUD），唯一new_interest hook清route/_at_goal；framed._plan在选择新局部目标前仍看见未达旧bridge pending，因此错误终止。批准候选33仅该唯一hook显式取消旧桥，不追认成功、不记超时、不退款/清来源账，随后交父目标优先规划并保留全部当前joint安全门；尚待实现、回归、冻结和实机。普通basis目标未发现独立缺陷；额外同源page单槽预计命中窄，未增加共享缓存或放宽鲜度。
