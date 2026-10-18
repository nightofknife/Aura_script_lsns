# PC 功能模板资源

公共图片按功能归属保存，调用方在现有目录索引中保存识别参数。第一批覆盖车间公共控件和黑月原生商品公共 mask，第二批覆盖导航、交易、恢复和乘客资源，第三批覆盖角色、装备、卡框和数字。

| 公共资源（相对 `plans/resonance_pc/`） | 唯一生成入口 | 使用方 |
| --- | --- | --- |
| `templates/workshop/staffing/controls/edit.png`、`edit.mask.png` | `tools/build_reforming_center_templates.py` | 房间目的地校验、生产车间人员保存校验 |
| `templates/workshop/staffing/controls/select_prisoner.png`、`select_prisoner.mask.png` | `tools/build_reforming_center_templates.py` | 四种原料车间目的地校验 |
| `templates/workshop/staffing/controls/selection/{select_all,cancel_all,confirm,back}.png` | `tools/build_raw_workshop_templates.py` | 原料车间及生产车间通用人员选择流程 |
| `templates/commerce/shop/black_moon/native/controls/local_tab.mask.png` | `tools/build_black_moon_local_shop_templates.py` | 本地标签 selected/unselected 两种控件状态 |

车间继续使用原有 plan-relative `image`/`mask` 引用。黑月原生库 `templates/commerce/shop/black_moon/native/manifest.json` 的 `file`、`mask_file` 和 `files` 键统一相对计划根目录，读取器只允许访问该计划的 `templates/`。`tools/build_black_moon_local_select_templates.py` 同样按此契约读取原生库；其自身经校准的 96×96 资源位于 `commerce/shop/black_moon/calibrated/`，索引仍相对该资源库。

同一图片的阈值、搜索区域、点击位置、预处理方法仍由各调用场景定义。编辑标识的房间校验默认阈值为 0.92，人员保存校验为 0.90；房间匹配器继续使用原来的灰度匹配，不因目录整理启用原先未使用的 mask。四个原料房间各自保留截图来源、裁切坐标和业务身份。

生产车间人员编辑器的真实截图控件与 native 变体有不同外观和尺寸，继续独立保存。黑月校准库的 96×96 商品 mask 独立保存；商品 ID、选中及售罄识别仍由当前校准库与业务记录定义。旧整段扫描器退休后，原生库只保留准备控件与供校准生成器使用的 140 张 normal 商品源图，不保留旧 selected 商品变体、原生卡框及 120×119 识别 mask。

生成器应直接写入上述功能目录，并只枚举自身声明的输出文件。多个来源裁切若声称生成同一个公共控件，必须验证像素相同，不能由最后一次写入覆盖差异。调用方引用公共图片，不再复制一份。旧图片已删除，读取器不提供旧路径回退。

## 导航、交易、恢复和乘客

第二批资源相对 `templates/` 的目录如下：

| 功能目录 | 内容 |
| --- | --- |
| `navigation/global/` | 返回、城市主界面、列车主界面标识、档案入口 |
| `navigation/travel/`、`navigation/city/identity/` | 发车、到站、目的地、喇叭、城市身份与访问入口 |
| `navigation/warehouse/`、`navigation/trailer/` | 仓库入口和分类标签、拖车控件 |
| `commerce/trade/` | 商店、买卖议价、出售、结算、`products/<商品ID>/<available或selected>.png` |
| `commerce/books/`、`commerce/investment/` | 采购书控件、交易品投资资源库 |
| `recovery/fatigue/` | 疲劳页面、药品控件 |
| `recovery/bento/` | 便当页面、使用和返回控件、数量数字、恢复量数字、爱心便当素材 |
| `recovery/water/` | 气泡水控件、免费次数数字 |
| `passenger/` | 管理、招募、目的地、派遣、数量确认和收益结算 |

现有业务目录数据继续使用 plan-relative 引用；投资和拖车的库内 manifest 保持相对各自资源库的引用规则。路径检查始终限制在对应计划或资源库内，拖车加载器显式使用计划根目录，不依赖资源目录的层数。头像菜单中的档案入口由玩家状态读取与恢复流程共同引用 `navigation/global/profile_entry.png`。

生成入口同步为 `build_city_identity_templates.py`、`build_trade_product_templates.py`、`build_trade_goods_investment_templates.py`、`generate_trade_rebargain_template.py`、`generate_bento_count_templates.py`、`generate_bento_recovery_digit_templates.py`。仓库标签的来源记录虽注明历史生成器名称，当前仓库没有对应脚本；本次保留已有图片及来源证据。手工截图控件继续以原图为准。

数字的 work/love 样式、font/sample 变体，以及投资页面的尺寸和状态仍分别保存。历史截图 fixture 中的路径记录属于当时的生成证据，保持原字节；运行时只读取当前业务目录与新资源库。旧资源目录已经移除，不使用链接、旧目录副本或旧路径解析。

## 角色、装备、卡框和数字

| 功能目录 | 业务目录或生成入口 |
| --- | --- |
| `characters/portraits/`、`characters/controls/`、`characters/stars/` | `data/meta/player_characters.json` 显式身份、顺序与样本列表 |
| `characters/nameplates/scuffle/`、`characters/stand/scuffle/` | `data/meta/eternal_scuffle.json`；`tools/build_eternal_scuffle_assets.py` |
| `inventory/equipment/art/<图片归属ID>/` | 背包 `grid_100x60.png`、乱斗 `detail_200.png`、`icon_88.png`、`tips_88.png`；不同名称按业务 ID 单独保存 |
| `inventory/equipment/frames/scuffle/` | 乱斗装备占用框，品质及识别参数独立 |
| `inventory/digits/count/`、`inventory/digits/expiry/` | 数量与有效期分开；各自目录数据保存数字范围、阈值、分割参数 |
| `commerce/investment/cards/`、`commerce/investment/cards/masks/` | 投资状态及尺寸记录保持，只有同尺寸同像素文件共享 |
| `commerce/shop/black_moon/calibrated/` | 当前选品的商品、卡角、选中及售罄证据；`native/` 提供准备控件和 normal 商品生成输入 |
| `workshop/production/digits/` | `tools/build_raw_workshop_digit_templates.py`；list/selection 字体变体保持 |
| `navigation/profile/status_digits/` | `tools/generate_profile_status_digit_templates.py` |

`player_characters.json` 必须提供 `characters` 列表，每条记录包含 `character_id`、`name` 和有序 `templates` 引用。加载器及 `tools/sync_team_recommendations.py` 都读取显式角色身份，不再通过文件夹名发现角色，也不提供旧目录方式回退。配队同步在联网前验证角色 ID 非空且唯一。未列入目录的文件不参与识别。“弗兰”和“空”即使共享图片，仍是独立的业务记录，保持原有顺序与并列得分行为；新增样本应显式登记。

共享图片的前提是视图、模式、尺寸和解码像素均一致。背包图、详情图、图标、tips 图与文字名称分别处理；相同图片不代表两个装备 ID 相同。乱斗每名角色仍保留八个立绘逻辑槽位及对应 mask，`stand_sources` 保存各槽位的来源；不同来源共用图片时，资产记录的 `source_variants` 保留原始来源信息。

黑月原生 selected/soldout 卡框、锚点及 mask 随旧扫描器一起退休；当前校准库使用自己的卡角、选中和售罄标识。投资的 locked/selected/maximum 等状态不随卡框共享合并。数量数字仅去掉同一数字、同一样式的像素重复样本，各数字的所有独特形状及原识别参数保持；恢复数字本已按功能分类，本批保持原样。

当前仓库没有背包装备图、数量及有效期字库的生成器，本次保留其原图与目录来源。已存在的生成器直接输出新功能路径，并在测试临时目录内验证重复生成不会重建旧目录。完整原生提取和真实游戏验收应另行执行。

## 剩余资源收尾与重建约束

| 功能目录（相对 `templates/`） | 内容 |
| --- | --- |
| `navigation/rooms/{map,selection,destination,controls}/` | 房间导航、目的地标识和界面控件 |
| `workshop/production/controls/raw/`、`workshop/staffing/editor/` | 原料控件与当前生产人员编辑器；旧生产编辑器文字生成图已退休 |
| `commerce/shop/black_moon/native/{controls,items,sources}/` | 准备控件、normal 商品生成输入和来源记录；索引相对计划根目录 |
| `commerce/shop/black_moon/calibrated/{controls,items,masks}/` | 校准控件、商品和 96×96 mask；索引相对资源库 |
| `battle/scuffle/controls/`、`battle/{navigation,dispatch,combat}/` | 乱斗控件与战斗入口、派遣、体力提示 |
| `commerce/investment/cape_island/{controls,cards,masks}/` | 海角投资控件、卡片类别与等级、识别 mask |
| `recycling/controls/` | 回收入口、可回收与空状态 |

人员编辑器已删除 15 张不再被索引使用的数字、标签和保存提示旧图；刷新生成器只删除其明确拥有的废弃输出。黑月本地标签 selected/unselected 共用 `native/controls/local_tab.mask.png`；乱斗四种品质框保留各自底图和识别参数，共用 `inventory/equipment/frames/scuffle/occupied_mask.png`。

原料人数数字生成器必须具备 list 与 selection 两种完整原生字体；`--selection-font-export` 为必填。缺失、错误或损坏输入在输出写入前失败，不能用 44 张 list 字形覆盖完整的 77 张索引。乱斗完整生成先验证资产和目录，再发布；根据上一次目录的 `assets` 清理失效输出，保留其他生成器的文件。输入变化后变为共用图片、或共用图片归属 ID 改变时，旧图片不会残留；失败生成保留原先有效目录。黑月生成器也按前次目录清理自身废弃输出。

非深潜模板根目录不再散放 PNG，也不保留按任务命名的旧资源目录、副本或路径回退。历史截图 fixture 和来源字段中的旧路径仅作为证据保留。

从仓库根目录运行聚焦测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_functional_template_release.py tests/unit/test_product_workshop_staff_ui.py tests/unit/test_raw_workshop_ui.py tests/unit/test_black_moon_prepare_templates.py tests/unit/test_black_moon_local_select_vision.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_functional_template_batch2_release.py tests/unit/test_trade_product_template_assets.py tests/unit/test_trailer_service_assets.py tests/unit/test_player_recovery_pc.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_functional_template_batch3_release.py tests/unit/test_character_functional_templates.py tests/unit/test_inventory_functional_templates.py tests/unit/test_frame_digit_functional_templates.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_functional_template_closeout.py tests/unit/test_team_recommendation_sync.py
```

生成器回归测试在仓库内临时目录中验证输出引用、像素和重复生成的一致性；截图回放沿用已版本化的来源证据。离线回放与打包引用检查不代表游戏实机流程验收。深潜资源和调用链不在本批范围。
