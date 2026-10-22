# PC 功能模板与生成器

模板按功能所有者归档。任务、动作和识别场景通过元数据或显式路径引用；任务文件名不再决定图片目录。业务 ID、ROI、阈值、颜色空间、稳定次数及消费保护仍由各场景独立维护。

## 当前目录

`plans/resonance_pc/templates/` 的普通功能目录如下：

| 目录 | 内容 |
| --- | --- |
| `battle/` | 战斗导航、派遣与乱斗控件 |
| `characters/` | 显式角色目录引用的运行头像、星级和姓名模板 |
| `commerce/` | 交易商品及买卖控件、采购书、投资与黑月商店 |
| `inventory/` | 装备图、卡框、库存数量及有效期数字、词条刷新资源 |
| `navigation/` | 全局导航、城市身份、旅行、拖车、仓库和整顿中心房间 |
| `passenger/` | 乘客招募、管理、派遣、目的地及结算控件 |
| `recovery/` | 便当、疲劳恢复和气泡水控件及数字 |
| `recycling/` | 回收入口与奖励页面控件 |
| `workshop/` | 生产、配员和收菜资源 |

深潜模板统一收纳在 `deep_dive/`，原 `consciousness_deep_dive*`、`deep_dive_*` 七个目录作为其子目录保留。此次只增加父目录并同步引用，不整理深潜内部层级、不合并重复图片、不改变图片内容。

新版收菜的独占图位于 `workshop/collection/`，仍由 `data/meta/product_collection_templates_pc.json` 声明六个车间的编号、开放/选中/锁定身份及识别参数；共用房间导航位于 `navigation/rooms/`。3～6 号生产车间地图标题位于 `navigation/rooms/map/map_workshop_{3,4,5,6}.png`，进入后的页面标题由导航与收菜显式共享 `navigation/rooms/destination/physical_workshop_{3,4,5,6}.png`；两种场景各自保留阈值、搜索区域及编号判定参数。词条刷新位于 `inventory/affix_refresh/`，原装备 ID、字形库、预算和候选保留行为不变。原生地图几何、拖拽算法及 `data/meta/intercity_map_native.json` 保留新版实现。

配员模板按页面归档：`workshop/staffing/editor/` 保存编辑页标题、快捷按钮及返回，`selection/raw/` 和 `selection/product/` 分别保存原料车间与生产车间的选择页控件，`save_prompt/` 保存保存弹窗的应用和放弃控件。主图与 `.native` 识别变体随各自页面保存，不能因同名而合并两套选择页图片。共用的编辑、选择囚犯控件及 mask 仍由 `staffing/controls/` 持有。买入确认页标题位于 `commerce/trade/buy/preview_title.png`，属于运行模板。

## 身份与共享

同一物理图片允许多个业务身份显式引用，例如共享角色头像或装备图。识别时继续使用调用场景自己的参数和完整候选集合，不能由文件名推断业务身份。角色目录与配队同步工具从 `player_characters.json` 的显式 `characters` 列表读取身份，列表顺序仍决定同分样本的优先级。

目录只收纳当前生产者或目录文件实际拥有的资源。生成器清理此前输出时只处理自己的旧索引，不按功能目录整树删除其他生产者的文件。旧任务目录、旧路径副本和路径回退均不保留。

收菜独占图片按页面与用途细分：`navigation/cards/` 保存六个车间的开放、选中、锁定卡片及身份裁片（54 张），`navigation/controls/` 保存导航入口、标题和选中框（4 张），`navigation/digits/` 保存白色与灰色编号（12 张），`room/` 保存 1、2 号标题及编辑标记（3 张），`capacity/` 保存仓满提示及确认、取消控件（3 张），`controls/` 保存领取、完成展示、返回和锁定提示等控件（11 张）。文件名保留，所有图片路径由正式 catalog 显式声明。

新增收菜素材时沿用正式元数据中的共享图片路径，不从历史审阅目录恢复已经去重的标题副本。主仓库及相邻 `Aura_script_lsns_devtools` 中均未找到生成这批收菜原生扩展的完整生产者，来源记录仅供追溯。后续恢复生产工具时，以 `product_collection_templates_pc.json` 各条目及变体的 `image` 路径作为输出位置，保留上述分类及共享关系；目录和 catalog 本身不足以重建原生素材，不能把本轮移动视为重新生成。

2026-10-22 第四批移动 87 张收菜图片，同步 catalog，并将收菜 catalog 和目录纳入现有递归发布测试的资源清单；业务 ID、ROI、阈值和来源记录不变。没有运行测试、模板匹配、生成器或打包流程，深潜目录及引用不变。

配员来源字体按 `(font_path_id, sha256)` 稳定去重；相同字体身份的额外来源差异记录在 `source_variants`，不同字节的字体保持独立。生成器退休旧配员路径时只处理旧 catalog 明确声明、当前不再引用且哈希未被外部更改的文件，不按目录递归清空。

## 导航配置来源

`src/actions/_navigation_templates.py` 维护玩家资料入口、全局返回和返回城市主界面的冻结配置；消费方派生自己的常量或获得独立布局副本。`data/meta/player_recovery.json` 的 `profile` 条目以 `{"profile": "navigation.profile"}` 显式引用共享配置，由恢复布局加载器展开，不在 JSON 重复维护图片、区域与阈值，也不允许同条目覆盖共享参数。

启程按钮的路径、尺寸、捕获区域、匹配方法、颜色与预处理选项、阈值统一来自 `data/meta/depart_button_template.json`。现有两个启程观察器共同要求 RGB 彩色 SQDIFF、无 mask、无预处理；加载器拒绝不支持的组合，避免普通出发与黑月退出观察产生差异。`_depart_button_vision.py` 延迟加载并缓存配置，保留由配置派生的 `REGION`、`THRESHOLD`、`TEMPLATE` 兼容导出；修改配置后需重启读取进程。轮询间隔、连续确认次数、取消及超时仍由原流程维护。

共享图片不要求共享识别配置。黑月返回按钮仍使用独立的彩色 SQDIFF 配置，贸易与回收使用原有灰度 CCOEFF 配置；便当恢复与消费的返回列车控件继续保留各自的匹配算法。战斗返回按钮图片及配置也保持独立。

2026-10-22 本批调整移动 23 张图片并同步生成器、catalog 和相关测试引用；没有运行测试、模板匹配、生成器或打包流程。目录及配置迁移不代表实机验收。

## 黑月两套资源

`commerce/shop/black_moon/native/` 保存准备控件、原生商品图及运行 manifest；完整提取来源报告位于 `development/template_sources/commerce/black_moon/native/sources/`。manifest 使用 schema 2，图片引用相对计划根目录。运行准备观察器只加载所需六个控件。旧单体采购的卡片锚点、选中态专属图和旧解析入口已退休。

`commerce/shop/black_moon/calibrated/` 保存分阶段扫描所需的校准控件、商品身份样本和公共 mask，manifest 图片路径相对本 bank。GUI 商品图标也读取这套目录；发布回归逐商品检查应有图标能够解码。

原生生成器先在仓库 `.pytest_tmp` 中暂存，验证完整商品 ID、六个控件、声明文件集合、解码、哈希、控件裁切尺寸、商品图尺寸、RGB/L 通道和有效 mask，随后发布。不同商品或控件不能意外复用同一图片输出。两个本地标签状态仅在相同几何的 mask 像素一致时共用 mask，状态图片保持独立。发布或清理失败会回滚旧资源和目录文件。

## 运行模板与开发素材

运行 `templates/` 与 `data/meta/` 的资产索引只声明运行所需图片。独立的 `development/template_sources/` 保存未接入银行和原生备份，`development/template_previews/` 保存展示总览；发布选择器不收录 `development/`。详细说明见计划内 `development/README.md`。

| 开发目录 | 内容与生成约定 |
| --- | --- |
| `template_sources/characters/stand/scuffle/` | 1,373 张未接入当前乱斗识别的立绘与 mask，以及保存角色 ID、顺序、排名、逐帧来源的开发 manifest。运行 catalog 不再含三个 `stand_*` 字段及其图片索引；乱斗仍使用姓名模板识别角色。 |
| `template_sources/inventory/affix_refresh/` | 23 张校准前原生字形。开发 manifest 保存原始规格、运行图及 JSON pointer 对应关系；运行 manifest 不再声明 `native_file` 或备份图片。生成器默认分开输出，临时 `--output` 的备份默认进入同级 `<output>_sources`，也可指定 `--development-output`。 |
| `template_sources/navigation/profile/status_digits/` | 尚未接入用户信息运行识别的 11 个数字/斜杠字形，以及 manifest、README。生成器默认写开发目录。 |
| `template_sources/recovery/bento/love/` | 13 张合成便当用原生食物、背景及有效期精灵和来源 manifest。运行 `items` 不再声明 `native` 备份路径。 |
| `template_sources/commerce/black_moon/native/sources/` | `native_ui.json` 与 `native_assets.json` 完整提取报告；完整生成、准备控件更新与 extract-only 均采用开发输出路径。 |
| `template_previews/recovery/bento/love/` | 菜品与角色两张展示总览及其独立 manifest。 |

便当生成器位于旁边的 `Aura_script_lsns_devtools/tools/build_bento_assets.py`，本批同时修正了它的旧运行目录输出并分离来源与预览。重新生成时保留当前 scanner 配置及同 ID 菜品的恢复值、星级，避免覆盖已有阈值。运行锚点 `templates/recovery/bento/love/native/card_background_1280.png` 保留；配员 `.native` 变体、黑月 native 准备控件、装备各场景视图也继续作为运行资源，不按文件名统一外移。

本批只移动已收录图片并拆分元数据，没有重建私有游戏资源。相关生成器及测试源码已同步，但没有执行测试、模板匹配、生成器或打包；深潜及其依赖不变。

## 匹配时的资源复用

通用 `VisionService` 的路径模板和 mask 复用服务实例内的解码及预处理缓存：最多 128 个资产、64 MiB 像素、每资产 8 种灰度/预处理配置，按 LRU 淘汰。缓存使用词法规范的绝对路径和文件的修改时间、创建/变更时间、大小及文件身份区分版本，每次对原始路径执行 stat，更新、删除或链接目标变化时失效；避免每次命中都完整解析路径。加载或准备失败不缓存。阈值及匹配算法不属于图片预处理配置。`load_template_image()` 返回只读 RGB/灰度快照，inspect 调试预览共享这一缓存后转 BGR，避免匹配后再次读文件；缺失或损坏时仍附带空预览和原始参数。`load_image_file()` 仍是直接读取原图的接口。

收菜专用的 `_product_collection_matching.py` 拥有独立的 160 项文件版本缓存，保存不可写的模板快照及延迟准备的灰度、标准差；文件更新后重新加载，准备期间文件变化则明确失败，不使用共享房间模块返回的可写数组作为缓存依据。`observe_frame()` 在当前同步观察内保留 ROI 灰度和原始匹配分数，并在首次使用后固定模板快照、复用根及父目录解析；下一次观察重新检查文件版本、删除和链接重定向。嵌套同帧观察共享，不同帧独立，退出或异常时释放；阈值仍逐次独立判定，命中结果每次重新构造，不跨截图复用。资源预检仅在单次调用内共享目录解析，每次预检仍检查全部文件。收菜因此复用导航、弹窗及领取控件的当帧证据，目标编号与其余五个编号各匹配一次，保留原编号差值及亮度判定。共享房间匹配器及其单次 `room_hits()` ROI 复用由独立优化批次维护，收菜不再依赖该模块新增缓存接口。

角色头像和星级素材一次解码完成尺寸检查及颜色转换，角色入口的灰度在本次 catalog 中持有；仓库入口在一次入仓操作内按需加载。砍价同轮的按钮与再交涉识别共用一张 ROI 截图及其灰度，各自保留阈值、未知状态与连续帧判定；只有显式共享帧的调用才提前准备灰度，其他观察调用仍将原 RGB 数组传入视觉服务。派遣四组分类切换将等待并入已有轮询点击动作，保留 8 秒超时、0.2 秒间隔和后续断言。点击后的效果与页面稳定确认继续读取新截图。

本批只调整运行时资源复用与重复调用，未修改深潜专用代码及素材；通用视觉缓存也会用于深潜传入的普通模板路径，像素准备参数与匹配规则不变。后续按用户授权完成测试、新旧差分和离线耗时对比，记录见 [模板复用优化验证记录](resonance-pc-template-matching-validation.md)。截图仍由测试夹具或固定数组提供，计时不包含真实游戏截图、动画等待和输入。

后续将收菜普通文件的目录边界判定按当前观察共享，特殊路径及叶文件链接仍执行完整检查；另外用两个各限 512 项的缓存复用纯词法 Path 对象，保留原始路径拼写，不缓存文件状态或目录解析结论。每次新观察仍检查替换、删除和链接目标变化。原因、异常差分与三版本耗时见 [收菜路径检查后续验证](collection-template-path-optimization.md)。

Windows CPython 3.12 上，短且无特殊名称的盘符路径采用一次完整原生规范查询，省去仅用于验证前缀移除的第二次查询；输入、输出都受保守句法检查约束，其他情况回原目录解析。cwd、解析后 stat、环路错误转换及文件版本检查保留，新增目录仍查询完整原始路径。详见 [目录查询去重与验证](collection-directory-checks-validation.md)。

## 生成与验证入口

保留新版工具分组入口，通过 `python -m tools assets --help` 查询可用生成器。主要生产者包括：

- 黑月：`build_black_moon_local_shop_templates`、`build_black_moon_local_select_templates`、`build_black_moon_sold_out_template`。
- 车间与房间：`build_reforming_center_templates`、`build_raw_workshop_templates`、`build_raw_workshop_digit_templates`、`build_product_workshop_staff_templates`。
- 交易、城市与乱斗：`build_trade_product_templates`、`build_trade_goods_investment_templates`、`build_city_identity_templates`、`build_eternal_scuffle_assets`。
- 数字与词条：`generate_bento_count_templates`、`generate_bento_recovery_digit_templates`、`generate_profile_status_digit_templates`、`build_affix_refresh_templates`。

手工采集可使用 `python -m tools.quick_template_capture --plan resonance_pc --category commerce/trade/controls`，将新图片存入计划模板树内指定分类。分类模式共用模板根目录下的 `_template_library.yaml`，以相对路径登记图片，注册的递归模板库也以模板根为起点；未指定分类时保留原有输出规则，`--category` 不能与 `--output-dir` 同用。同名文件已存在时提示更名，不覆盖已有图片。

旧 quick_fill 的专用 UI、生成器和元数据已经删除；当前生产配员与新版收菜各自维护目录契约。校准黑月生成器每批只生成一次公共 mask。生成需要的私有游戏资源必须由开发者提供；本轮迁移保留已打包素材，通过受控测试源验证生成路径、完整覆盖和失败回滚，没有重新提取外部游戏资源。

截图房间生成器只拥有原来的 24 个房间条目。重新构建时保留现有目录中独立原生生成的 3～6 号生产车间完整条目及其图片、原注释和不支持原因；不能用旧截图集合覆盖新版收菜新增内容。现存目录损坏或结构非法时会在写输出前失败，新建目录则可正常生成原有房间集合。

测试、验证和生成回归都从仓库根目录执行。临时目录位于本仓库 `.pytest_tmp`；非 pytest 工具需将 `TEMP`、`TMP`、`TMPDIR` 指到其中的专属目录。离线资源、行为和发布选择回归不代表游戏实机验收。
