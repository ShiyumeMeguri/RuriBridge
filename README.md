# RuriBridge

> ## 🟧 Blender 在中间,另外两个补它的短板
>
> **建模在 Blender**,它是模型唯一的作者。另外两个软件不改模型,各自把自己那半交回来:
>
> | | 拿走 | 交回来 |
> |---|---|---|
> | **Substance 3D Painter** | 绘制表面 | 通道贴图 |
> | **Cascadeur** | 绑定 + 当前动作 | 做完的表演 |

跨进程的载荷落在一个共享的「竞技场」目录里:一份定长控制块(每条通道一个 seqlock 槽)加上
不可变的 generation 目录,写满才发布,所以读者永远不用对载荷加锁。文件都带
`FILE_ATTRIBUTE_TEMPORARY`,对面紧接着就读,页从缓存出,不走一遍写回再读出。

**检出就住在 Blender 的 addons 目录里**,仓库根即插件;Painter 用目录联接指回来,
Cascadeur 收一份生成的入口脚本。一份代码,不复制、不分叉。

```
<Blender scripts>/addons/RuriBridge/
  __init__.py        宿主检测 + 分派
  Kernel/            宿主中立,纯标准库:花名册、主题、会话、竞技场、通道、记录、命令行
  Host/
    Blender/         唯一允许 import bpy 的地方
    Substance/       唯一允许 import substance_painter / PySide 的地方
    Cascadeur/       唯一允许 import csc 的地方
```

## 怎么用

**什么都不会自己发生。** 每一次过桥都是有人在某一边按了按钮。

Blender:3D 视图 N 面板 → `RuriBridge` 页

- **Send Mesh**:把绘制表面发给 Painter。Painter 没开工程就用它新建一个;开着就换网格,
  每个纹理集的图层一层不动。这一发会在已经画过的工程里**新建纹理集**时先问一句:新集上什么都没画,
  如果是某个材质改画进了新集,它的面在原来那个集里的画就不再显示(图层还在)。
  没有材质的面不发 —— 桥不替你建材质(建出来的名字没人起过,还会改掉物体的渲染);面板列出这些物体。
- **Sync Material**:把每个纹理集的着色器和参数交给 Painter(见下文「材质:身份决定搬多少」)。
  只给着色器和参数,不给贴图;要连贴图一起,在 Painter 里按 Sync All Material / Sync Selected Material。
- **Pull Textures**:请 Painter 把所有纹理集导出到这个 .blend 的 `textures` 文件夹,并接进
  画进那个纹理集的材质里;生成材质按它自己着色器的贴图布局重新拼好再换进它的记录(见下文「贴图」)。
- **Pull Selected Layer**:只要 Painter 里选中的那一层,单独导出。进来的是图片,不接进材质 ——
  一层不是材质该显示的全部,接进去就把完整结果顶掉了。
- **一张表**:每个材质画进哪个纹理集,两边都能改。

Painter:停靠面板 `RuriBridge`

- **Update Mesh**:只更新网格本身 —— 请 Blender 发表面过来换进去,图层不动,别的什么都不改
  (和 Blender 那边按 Send Mesh 一样)。
- **Sync All Material** / **Sync Selected Material**:请 Blender 交出画纹理集的材质 —— 着色器、参数,身份相同时连贴图
  一起(见下文)。All 是这个工程里每个纹理集;Selected **只管当前选中的那一个**,别的一个都不碰。
- **Send All Textures** / **Send Selected Textures**:把每个纹理集 / 只把选中的那个纹理集导出到 Blender
  文档的 `textures` 文件夹,Blender 把它们接进画那些纹理集的材质。
- **Pull a Blender image into the selected layer**:把 Blender 材质用到的一张图放进选中的图层,
  作为遮罩或参考填充。Blender 没有图层,所以从那边来的东西只可能是这两种形状,而且只碰选中的那一层。
  选中的接不了(没选、选了好几个、参考却选了绘画层)就**新建一个填充层**装它,放在选中层上方
  (没选就放最上面),只开底色通道 —— 不拿缺省值盖住下面各层的其他通道。
  拉的是磁盘上的那份文件:Blender 里改过还没存的图,先存盘再拉。
- **同一张表**:纹理集 / 图层数 / 由哪个 Blender 材质画。有图层却没有材质画它的纹理集标成橙色 ——
  下一次换网格会在它这里停下;要放弃它,勾上 **Drop**(只对下一次换网格有效)。

## 纹理集就是名字

材质画进哪个纹理集,记在材质的自定义属性 `ruri_bridge_identity` 上。第一次过桥时抄下材质
当时的名字,从此不动 —— Blender 里改材质名只改标签,不会让 Painter 以为来了个新材质。

- 多个材质可以画进同一个纹理集(一个材质拆成两个、共用一套不重叠 UV 的情况)。
- 属性设成空字符串 = 这个材质不进 Painter(描边、毛发壳这类渲染专用材质)。
- Painter 里的纹理集名不对(外包工程常见,比如 `pCube2SG`):在表里给它选 Blender 材质。
  那个材质已经有人画进别的纹理集就并进去;没有的话,桥请 Painter **把纹理集改名成材质名**
  —— 改名是 Painter 里唯一保留全部图层的编辑。

## 绘制表面:OBJ,落在工程自己的坐标系里

发过去的是**画的那个表面**:静止姿势的基础网格,带建模修改器(镜像、三角化),不带渲染时才有的
(骨骼姿势、几何节点长出来的描边壳和毛发层)。按 Blender 渲染用的那套 UV,按原始多边形写,
让 Painter 自己三角化。

**工程有自己的坐标系,一生不变。** Painter 把三平面 / 平面 / 球形 / 扭曲投影都放在工程表面
第一次进来时的那个包围盒里,换网格时把笔触从旧表面上的位置重投影到新表面上的同一处。
**保留笔触**换网格就保住这个框;不保留,所有投影和多边形填充的遮罩就整体挪位 —— 哪怕新表面
只在远处多了一个三角形。Painter 只在单位和比例一致时才肯保留笔触,所以表面必须按工程的坐标系
送来:

- 桥新建的工程以厘米计(`scale = 100 × 场景单位`),坐标系写进工程元数据 `RuriBridge/frame`,
  随 .spp 存盘。
- Painter 把坐标系放在自己的在场记录里,Blender 每次发网格都写进这个坐标系。
- 别人做的工程(没有坐标系)会被拒绝,而不是猜一个:猜错会让所有 3D 画的东西挪位。
  这种工程要先量出它原本的坐标系、写进元数据(一次性),之后就和桥建的工程一样。

保留笔触也有搬不动的:**多边形选区是按纹理集里的三角形记的**,一个面换了纹理集、或者四边形
换了对角线,它那份选区就跟着走了。这是 Painter 自己的限制,有没有桥都一样。

选 OBJ 而不是 glTF:glTF 的材质描述会被 Painter 的导入器变成每个新纹理集上一层没人画过的
「导入的颜色」(金属度还是 1)。OBJ 旁边那份 `.mtl` 只列材质名,别的什么都不说。

## 材质:身份决定搬多少

着色器生成器给每一条腿的产物烙同一个**身份**:风格参数面(part 词汇 × 纹理槽 × uniform 名与类型)
的散列。Blender 的生成材质把它写在 `ruri_shading.identity`,Painter 货架上的着色器把它写在旁边的
`<名字>.manifest.json`。

- 一个纹理集由**和它同名的材质**代表(多个材质画进同一个纹理集时,同一个着色器实例只能有一行值)。
- Painter 货架上同名着色器的身份**与材质声明的相同** → 纹理集换上这个着色器(每个纹理集一个自己的
  实例,共用实例装不下不同的值),整行参数原样写入。
- **不同**(别的着色器、同一着色器的另一代、或者没烙身份的旧拷贝)→ 不换着色器,只写两边**同名**的
  参数,其余的不猜,写进 Painter 日志。

货架上的着色器和清单是一个产物,由导入器一起同步进货架;只同步了着色器、清单还是旧的,身份就对不上,
桥会退回只写同名参数并说明原因。

**从 Painter 要(Sync All Material / Sync Selected Material)、且身份相同时,材质的贴图也一起过来**(也是给新纹理集
打底的办法,比如补回一张脸用 Sync Selected Material 只动那一个集):清单的 `inputs` 写着
着色器的每个输入是材质哪张贴图的哪几个分量、过什么运算 —— 导入器把游戏材质导进 Painter 读的也是这张表。
Painter 的 Python 做不了逐像素运算,所以按表开单请 Blender 切图(分量拷贝、取反、Unity 法线解包),
回来的图这样落地,**一层不删**:

- 可画的通道装进集**最底下**一个桥自己的填充层 `Blender: <材质名>`:下次同步找到它重新填,不叠第二层;
  已有的图层都在它上面,原来盖住哪里还盖住哪里。栈里没有的通道按清单写的格式加上,用户通道以输入的
  语义名作标签(和导入器一致;Painter 导出时按这个标签给文件起名)。
- 和 Painter 自己的烘焙合成的输入(切线法线、AO)当这个集的网格图 —— **只在集里还没有时**才设,
  别人烘的图不归桥换。
- 着色器整张读的图(渐变、查找表、SDF、打包剩下的位)写进这个集自己的着色器实例的参数。
- 字节相同的图不重复导入(哈希记在工程元数据里)。
- Painter 是**用到时**才从导入的源文件读图(存盘时才嵌进工程),而送来的图是传输件、会被回收;
  所以导入前先在 `%LOCALAPPDATA%\RuriDccBridge\painter_imports\<进程号>` 留一个硬链接,留到工程关闭。
  Painter 不在了的文件夹,下次有 Painter 用到时清掉。

## 贴图

导出的贴图写进 .blend 旁边的 `textures` 文件夹(偏好设置里可改名),Blender 直接引用这些文件,
所以贴图跟着 .blend 走。色彩空间由知道通道格式的一侧写进记录:`sRGB8` 是唯一按 sRGB 编码存储的
格式,其余一律按原值用(Blender 的 `Non-Color`)。

**生成材质不吃散通道。** 它采样的是生成时那套贴图(底色图 alpha 里装不透明度、打包法线、光泽图的
alpha 是 1−粗糙度……),按自己的记录读。哪个通道进哪张图的哪个分量、过什么运算,是 Painter 货架上
着色器清单的 `reads` —— Painter 侧着色器边画边从通道拼出这些图用的就是这张表;身份与材质声明一致时,
Painter 把它连同「每个输入在这次导出里是哪张图」写进贴图记录,Blender 逐像素照做,拼成
`<纹理集>_<槽>.png` 写进 `textures`,换进材质记录,材质运行时跟着记录重新接线。不发明数据:

- Painter 栈里**没有**的通道(比如外包工程没建高光强度、自发光遮罩),那一分量保留材质自己贴图里的值;
- 清单说原样保留的分量(一张图打包剩下的位),取材质自己的贴图;
- 一张图用到的 Painter 通道**全是一个值**(没画过),材质保留自己的那张;整个纹理集都这样,就报「没画」。

身份不一致(或货架上没清单)时不拼,报原因 —— 用别的一代着色器的表拼,每个分量都会错位而且一声不响。

## 动作:送去 Cascadeur,做完拿回来

N 面板的 Cascadeur 一节:把选中的绑定和它当前的动作一起发过去(骨架跟着表演走),在 Cascadeur
里做,再取回来 —— 取回的曲线**按骨名落到已有的绑定上**,临时导入物当场删掉。Cascadeur 不驻留:
桥召唤它跑一条命令,它做完就退出。

两个方向都只用 glTF(Cascadeur 自带 `csc.glb` 两个方向的门),一份模型不存两种编码。

## 装进三个宿主

```bash
python -m RuriBridge.Kernel.cli install ^
    --painter-plugins "<Painter 用户 python>/plugins" --enable-painter-plugin ^
    --cascadeur "<Cascadeur>/cascadeur.exe"
```

安装器走一遍花名册,每一行自己说怎么装:检出本体(Blender)、目录联接(Painter)、往它自己的
命令目录写一扇门(Cascadeur)。`--enable-painter-plugin` 写 Painter 的 `launch_at_start`,
**Painter 必须关着**。

所有应用用同一个会话名(默认 `default`,可用 `RURI_BRIDGE_SESSION` 覆盖)就在同一块竞技场上;
会话根默认 `%LOCALAPPDATA%\RuriDccBridge\sessions`,可用 `RURI_BRIDGE_ROOT` 覆盖。

## 命令行

```bash
python -m RuriBridge.Kernel.cli status                 # 控制块:每个通道的代次/确认/载荷字节
python -m RuriBridge.Kernel.cli inspect --channel mesh@Blender
python -m RuriBridge.Kernel.cli verify-mesh            # 重读发布的表面并逐项判定
python -m RuriBridge.Kernel.cli textures --into <目录>  # 列出 Painter 最新一批贴图,可另存
python -m RuriBridge.Kernel.cli sessions / remove
python -m RuriBridge.Kernel.cli publish-mesh   --blender <blender.exe> --blend <场景>
python -m RuriBridge.Kernel.cli request-export --blender <blender.exe> --blend <场景>
python -m RuriBridge.Kernel.cli pull-textures  --blender <blender.exe> --blend <场景>
```

`verify-mesh` 是判据不是打印:每个面角都落在顶点 / UV / 法线表内,每个物体画进每个纹理集的面数
与记录一致,声明的包围盒真的包住位置,记录带着坐标系。

驱动动作都是**让 Blender 去发**:每条通道只有一个写者,命令行变成第二个写者就会和插件抢代次号。

## 通道与记录

通道名是算出来的:`<主题>@<说话的人>`。谁能说谁能听不是表,是能力的连接 —— 加一个应用是
花名册里多一行。

| 主题 | 谁能说 | 谁能听 | 语义 |
|---|---|---|---|
| `mesh` | Blender | Painter | 队列 |
| `tex` | Painter | Blender | 队列 |
| `anim` | Blender、Cascadeur | Blender、Cascadeur | 队列 |
| `shade` | Blender、Painter | Blender、Painter | 状态 |
| `here` | 三个都能 | 三个都能 | 状态 |
| `ask` | 三个都能 | 三个都能 | 队列 |

**队列**是不可变 generation 目录,未确认的不丢;**状态**是一个槽,后写覆盖前写。确认位按听众
分开,发布者回收时取所有真正在听的人的最小值,并保住每个听众最后确认的那一代。

记录格式带版本号;读到别的版本直接拒绝,不兼容旧形状。

## 已知边界

- UDIM 摄取按 Blender 的 `<UDIM>` 平铺图实现了,但没有 UDIM 工程可跑,未实测。
- Windows only,这是机制本身决定的(Win32 文件属性与映射语义)。
