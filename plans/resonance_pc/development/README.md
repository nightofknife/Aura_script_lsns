# 模板开发素材

此目录保留生成、追溯和离线研究需要的素材。运行时不依赖此目录；发布选择器只收录指定的运行目录，不将 `development/` 复制给玩家。不要把这里的文件移到同样会随包发布的 `assets/` 或 `data/meta/`。

## 目录

| 目录 | 用途 | 生产者 |
| --- | --- | --- |
| `native_projection/` | 深潜原生几何模块、失败实机证据和换机交接（尚未通过四视角识别验收） | 原生投影开发实验 |
| `template_sources/characters/stand/scuffle/` | 当前未接入识别的乱斗立绘序列、mask、角色来源及开发 manifest | `tools/build_eternal_scuffle_assets.py` |
| `template_sources/inventory/affix_refresh/` | 校准前的原生字形备份、运行模板对应关系和来源快照 | `tools/build_affix_refresh_templates.py` |
| `template_sources/navigation/profile/status_digits/` | 未接入用户信息运行识别的数字银行 | `tools/generate_profile_status_digit_templates.py` |
| `template_sources/recovery/bento/love/` | 合成便当运行模板所用原生精灵及来源索引 | 外部 `Aura_script_lsns_devtools/tools/build_bento_assets.py` |
| `template_sources/commerce/black_moon/native/sources/` | 黑月原生资源提取和 UI 来源报告 | `tools/build_black_moon_local_shop_templates.py` |
| `template_previews/recovery/bento/love/` | 菜品和角色的展示总览及图片索引 | 外部便当生成器 |

乱斗与便当开发 manifest 的资产路径相对计划根目录；词条刷新和状态数字银行的资产路径相对各自 manifest。词条刷新的 `runtime_manifest` 明确指向对应运行 manifest，其他来源索引也保留运行 catalog 或运行模板的对应关系。历史来源快照中的旧路径只用于追溯，不是运行回退路径。原生字体文件和私有游戏安装目录仍由开发者提供，不在这里新增导出。

## 运行边界

运行 `templates/` 继续保留黑月 native 准备控件与 calibrated 扫描资源、便当 `native/card_background_1280.png` 锚点、配员的 `.native` 识别变体，以及不同场景的装备图和数字银行。是否属于开发素材由消费者决定，不能仅按 `native`、`preview` 或字号命名判断。

便当生成器重新构建时保留已有 scanner 配置及同 ID 菜品的恢复值、星级，只刷新生成图片路径；不会因改正旧输出目录而恢复历史默认阈值。乱斗生成器在两个 catalog 发布前检查旧运行立绘是否已被完整保留，无法证明时拒绝退休；开发图片或 manifest 发布失败时仅回滚本轮新建文件。

本批只移动已收录素材并调整代码、元数据和测试引用，没有运行生成器、测试、模板匹配或打包。深潜目录及其依赖不属于本批范围。
