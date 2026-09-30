# 第一位面灵感初始分布的静态证据

只读分析本机 Patch/BinaryConfig 与 Lua 5.3 字节码；没有操作游戏、调用游戏 API 或执行客户端 Lua。

## 已确认

- CubeRogueFactory 89400001：specialNum=2，actionNum=6，cubeSize=3，initialX/Y/Z=2/2/1。
- cubeList 共54格：顶面中心 (2,2,1) 无事件；底面中心 (2,2,6) 使用 BOSS 格事件包 86100362；其余52格全部使用同一常规格事件包86100103，没有面/边/角/中心区别。
- 86100103 含6个候选事件包且各 weight10；86100362 仅含一个 BOSS 事件包。
- certainlyGenerateList=[]。
- specialGenerateList=[{id:12601542,weight:10},{id:12601540,numMin:1,numMax:2,weight:10}]。
- Tag类型常量映射12601542=HardBattle（精英战斗），12601540=NormalBattle（普通战斗）。
- FactoryRegister/Propertys/CubeRogueFactory.lua 的字段描述：specialNum 是“特殊道具生成数量”；certainlyGenerateList 是“特殊道具必定生成列表|必定会在该类型上生成”；specialGenerateList 是“特殊道具生成列表|生成特殊道具的生成方式”。所以灵感生成受节点类型配置约束，不能说客户端证实在52格直接等概率投放。
- 对 Patch/Script 执行二进制文本搜索，specialGenerateList/certainlyGenerateList 只出现于上述配置字段元数据，没有找到客户端生成实现。
- UICubeRogueMainDataModel.InitRubik 原始43–130行直接读取服务器缓存 netData.faces，然后调用 rubikCube:SetData；RubikCube.SetData 原始327–343行把各格 netData、tp_id、eid 和 item 赋值；Generate 原始149–164行只是创建几何、GenerateFace、GenerateDetail。

## 无法确认

- 生成服务器是否对事件布局使用额外空间约束，及灵感在候选战斗节点中的具体选取和概率。
- 根据相同52格事件包，可以推断若服务器对这些格子的处理可交换，边际灵感位置可视为均匀；该条件未从客户端证明。最终数字需要标明“52格中任选两个不同格等概率”的布局模型假设，不能宣称实测关卡平均。
- 特殊生成表的 numMin=1/numMax=2 从字面提示至少1枚在普通战斗节点，另一枚在普通或精英战斗节点；服务器算法不可见，尚不足以确定联合法则。

## 缓存

config.json 是89400001完整嵌套配置；property_strings.txt 保存上述字段中文说明；package_records.json 保存常规/BOSS事件包；evidence.txt 记录客户端相关函数常量。完整客户端字节码未复制到仓库。
