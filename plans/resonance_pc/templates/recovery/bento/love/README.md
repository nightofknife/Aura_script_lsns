# 便当柜匹配素材

由独立仓库 `Aura_script_lsns_devtools` 的 `tools/build_bento_assets.py`
从当前安装包导出（通过 `--project-root` 指定 Aura 源码目录）。配置位于
`plans/resonance_pc/data/meta/love_bento.json`。

- `food/`：11种爱心便当，原生底图与食物组合后裁取中央区域。
- `roles/`：98个具有有效 FoodList 引用的角色配置条目，原字体与底图组合。
  包含安装包中配置的条目，不代表全部角色已经实装或当前账号拥有。
- `days/`：0–9天的图标、数字与底图组合；范围不代表保质期上限。
- `masks/days.png`：只比较中央数字区域，排除外圈箭头。
- `native/`：原生图片与参考分辨率卡片底图。
- `catalog_preview.png`、`roles_preview.png`：展示用总览，不用于匹配。

参考客户端分辨率为1280×720。JSON的三个裁剪框使用左、上、右、下坐标，
相对于缩放后的卡片底图左上角，不是整个窗口或食物模板左上角。
原生RectTransform坐标以Node_LoveBento中心为原点，Y轴向上。

姓名取自UnitFactory.name；菜品取自FoodFactory；角色与菜品关联取自
UnitFactory.FoodList的明确ID引用。字色、字体引用、字号、对齐和位置来自
HomeFood预制体。数字遮罩是匹配策略，不是游戏自带素材。

识别时按本轮约定使用角色ID、便当ID、剩余天数去重。角色菜品关联可辅助
核对识别结果。扫描、拖动与去重逻辑位于
`plans/resonance_pc/src/actions/love_bento_pc_actions.py`。

工作餐与爱心便当分别保存为 `recovery.work_meals`、`recovery.love_bentos`，
没有旧 `recovery.bento` 读取兼容。进入页面即从顶部读取，不执行回顶。
爱心便当只在完整扫描完成后保存；工作餐独立保存。

列表ROI由预制体锚点换算至参考分辨率；截图区域外扩6像素，三个识别框
各预留4像素搜索余量。预制体没有绑定标准滚动条，因此通过有界拖动后的
多次静止确认结束，不使用“本屏无新增条目”作为结束条件。

文字使用Pillow离线栅格化，不能保证与Unity逐像素一致。当前未运行匹配测试，
未标定阈值；不得把生成素材当作已确认的识别准确率。
