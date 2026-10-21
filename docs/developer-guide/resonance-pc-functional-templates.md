# PC 功能模板与生成器

模板按功能所有者归档。任务、动作和识别场景通过元数据或显式路径引用；任务文件名不再决定图片目录。业务 ID、ROI、阈值、颜色空间、稳定次数及消费保护仍由各场景独立维护。

## 当前目录

`plans/resonance_pc/templates/` 的普通功能目录如下：

| 目录 | 内容 |
| --- | --- |
| `battle/` | 战斗导航、派遣与乱斗控件 |
| `characters/` | 显式角色目录引用的头像、星级、姓名和立绘样本 |
| `commerce/` | 交易商品及买卖控件、采购书、投资与黑月商店 |
| `inventory/` | 装备图、卡框、库存数量及有效期数字、词条刷新资源 |
| `navigation/` | 全局导航、城市身份、旅行、拖车、仓库、整顿中心房间及状态数字 |
| `passenger/` | 乘客招募、管理、派遣、目的地及结算控件 |
| `recovery/` | 便当、疲劳恢复和气泡水控件及数字 |
| `recycling/` | 回收入口与奖励页面控件 |
| `workshop/` | 生产、配员和收菜资源 |

深潜及其共用依赖继续保留原路径与字节。`consciousness_deep_dive*`、`deep_dive_*` 等目录不属于本轮普通功能迁移范围。

新版收菜的独占图位于 `workshop/collection/`，仍由 `data/meta/product_collection_templates_pc.json` 声明六个车间的编号、开放/选中/锁定身份及识别参数；共用房间导航位于 `navigation/rooms/`。词条刷新位于 `inventory/affix_refresh/`，原装备 ID、字形库、预算和候选保留行为不变。原生地图几何、拖拽算法及 `data/meta/intercity_map_native.json` 保留新版实现。

## 身份与共享

同一物理图片允许多个业务身份显式引用，例如共享角色立绘或装备图。识别时继续使用调用场景自己的参数和完整候选集合，不能由文件名推断业务身份。角色目录与配队同步工具从 `player_characters.json` 的显式 `characters` 列表读取身份，列表顺序仍决定同分样本的优先级。

目录只收纳当前生产者或目录文件实际拥有的资源。生成器清理此前输出时只处理自己的旧索引，不按功能目录整树删除其他生产者的文件。旧任务目录、旧路径副本和路径回退均不保留。

## 黑月两套资源

`commerce/shop/black_moon/native/` 保存准备控件、原生商品图及来源记录，manifest 使用 schema 2，图片引用相对计划根目录。运行准备观察器只加载所需六个控件。旧单体采购的卡片锚点、选中态专属图和旧解析入口已退休。

`commerce/shop/black_moon/calibrated/` 保存分阶段扫描所需的校准控件、商品身份样本和公共 mask，manifest 图片路径相对本 bank。GUI 商品图标也读取这套目录；发布回归逐商品检查应有图标能够解码。

原生生成器先在仓库 `.pytest_tmp` 中暂存，验证完整商品 ID、六个控件、声明文件集合、解码、哈希、控件裁切尺寸、商品图尺寸、RGB/L 通道和有效 mask，随后发布。不同商品或控件不能意外复用同一图片输出。两个本地标签状态仅在相同几何的 mask 像素一致时共用 mask，状态图片保持独立。发布或清理失败会回滚旧资源和目录文件。

## 生成与验证入口

保留新版工具分组入口，通过 `python -m tools assets --help` 查询可用生成器。主要生产者包括：

- 黑月：`build_black_moon_local_shop_templates`、`build_black_moon_local_select_templates`、`build_black_moon_sold_out_template`。
- 车间与房间：`build_reforming_center_templates`、`build_raw_workshop_templates`、`build_raw_workshop_digit_templates`、`build_product_workshop_staff_templates`。
- 交易、城市与乱斗：`build_trade_product_templates`、`build_trade_goods_investment_templates`、`build_city_identity_templates`、`build_eternal_scuffle_assets`。
- 数字与词条：`generate_bento_count_templates`、`generate_bento_recovery_digit_templates`、`generate_profile_status_digit_templates`、`build_affix_refresh_templates`。

旧 quick_fill 的专用 UI、生成器和元数据已经删除；当前生产配员与新版收菜各自维护目录契约。校准黑月生成器每批只生成一次公共 mask。生成需要的私有游戏资源必须由开发者提供；本轮迁移保留已打包素材，通过受控测试源验证生成路径、完整覆盖和失败回滚，没有重新提取外部游戏资源。

截图房间生成器只拥有原来的 24 个房间条目。重新构建时保留现有目录中独立原生生成的 3～6 号生产车间完整条目及其图片、原注释和不支持原因；不能用旧截图集合覆盖新版收菜新增内容。现存目录损坏或结构非法时会在写输出前失败，新建目录则可正常生成原有房间集合。

测试、验证和生成回归都从仓库根目录执行。临时目录位于本仓库 `.pytest_tmp`；非 pytest 工具需将 `TEMP`、`TMP`、`TMPDIR` 指到其中的专属目录。离线资源、行为和发布选择回归不代表游戏实机验收。
