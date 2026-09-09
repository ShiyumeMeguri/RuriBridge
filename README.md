# RuriBridge

> ## 🟧 Blender 在中间,另外两个补它的短板
>
> **建模在 Blender**,它是模型唯一的作者。另外两个软件不改模型,各自把自己那半交回来:
>
> | | 拿走 | 交回来 |
> |---|---|---|
> | **Substance 3D Painter** | 网格 + 材质 | 烤好的通道贴图 |
> | **Cascadeur** | 绑定 + 当前动作 | 做完的表演 |
>
> 目的只有一个:**让 Blender 变成顶级软件** —— 贴图交给 Painter、动作交给 Cascadeur,
> 而你从头到尾待在 Blender 里。

跨进程的载荷全部落在**同一批物理页**上:Win32 的文件映射就是 section 对象,两个进程映射同一份
就是同一批页,而且每个文件都带 `FILE_ATTRIBUTE_TEMPORARY`(求缓存管理器只要有内存就别写回)。
所以路径是**共享内存页的名字**,不是磁盘往返 —— 跨进程共享本来就需要一个名字。

**检出就住在 Blender 的 addons 目录里**,仓库根即插件;Painter 用目录联接指回来,
Cascadeur 收一份生成的入口脚本。一份代码,不复制、不分叉。

```
<Blender scripts>/addons/RuriBridge/    ← 检出本体,三个应用装的都是它
  __init__.py        宿主检测 + 分派,别的什么都不做
  Kernel/            宿主中立。不 import bpy / substance_painter / csc,纯标准库
    host.py          端口:桥对应用的全部要求 + 能力常量
    peers.py         花名册:三个应用各能答什么、驻不驻留、代码怎么进去
    topic.py         什么东西过桥,谁能说谁能听 —— 通道名从这里算出来
    session.py       一次附着:按主题发端点,不按通道
    arena.py         竞技场:控制块(seqlock 槽) + 不可变 generation 目录
    channel.py       单写者发布/订阅,确认位按听众分开
    record.py        线格式:记录形状与色彩空间判据
    glb.py           就地 GLB:先按计数排好版再映射,生产者一次写入即成品
    summon.py        把不驻留的应用叫来看一眼
    counterpart.py   写进 Cascadeur 命令目录的那扇门
    cli.py           无宿主的命令行:安装、观测、验证、驱动
  Host/
    Blender/         唯一允许 import bpy 的地方
    Substance/       唯一允许 import substance_painter / PySide 的地方
    Cascadeur/       唯一允许 import csc 的地方
```

## 怎么用

**装好之后你只需要按一个按钮。** 3D 视图 N 面板 → `RuriBridge` 页:

1. 面板顶上写着 Painter 有没有接上。**没接上也不用先开 Painter** —— 直接按 **Send To Painter**:
   网格写进竞技场,桥顺手把 Painter 启动起来,Painter 一开就把等着的那份网格接走并建好工程。
   Painter 的路径不用你填,从 Windows 注册表的 `App Paths` 读(那是安装器自己登记的),
   而且两边第一次连上之后 Painter 会把自己的路径告诉 Blender,存进插件偏好设置。
2. 从这一刻起**什么都不用再点**:你在 Painter 里画 → Blender 的贴图跟着变;
   你在两边改 shader 参数 → 另一边跟着变;你改几何、离开编辑模式 → Painter 重载网格。
3. 不想要自动的,把 **Live Sync** 关掉,下面那排手动按钮照常用。

Painter 那侧的停靠面板同理:写着 Blender 有没有接上,一个 **Live sync to Blender** 开关,
一个手动 **Send Textures To Blender**。

**唯一需要理解的概念是「会话」**:所有应用用同一个会话名(默认 `default`)就在同一块共享内存上。
会话目录名里带一个形状指纹(格式版本 + 通道表),所以换了构建就是**另一个目录**,
而不是去重建一个别的进程正映射着的文件 —— 那件事在 Windows 上做不到,以前的报错就是它。
面板里 `Channels` 那个折叠区能看到会话名、目录、和每条通道的实时代次 —— 平时不用管它。

一个人先开谁都行:先开 Blender 发网格再开 Painter、先开 Painter 再开 Blender、两边都开着,
三种顺序都成立。


## 为什么是这个机制

Windows 上**映射文件就是 section 对象**。两个进程映射同一个文件,寻址的是同一批物理页 ——
Blender 写进映射页的字节,就是 Painter 的导入器读出来的字节,中间没有序列化 / write /
read 这一轮。竞技场创建的每个文件都带 `FILE_ATTRIBUTE_TEMPORARY`,只要内存够,
缓存管理器就不会把它写回存储,所以这里的"文件"是一段两个进程都已经看得见的 RAM 的名字。

可变状态刻意做到极小:一份定长控制块,每个通道一个槽。载荷是**不可变的 generation 目录**
—— 写满了才发布,所以读者永远不需要对载荷加锁,只有槽里那几个整数被 seqlock 保护
(写者把序号变奇数、改、再变偶数;读者看到奇数或看到序号变了就重读)。

选 GLB 而不是 FBX / OBJ / USD,是因为 glTF 的二进制块是紧凑小端数组,**磁盘布局和顶点数组
的内存布局是同一个**。所以整份文件能在一个字节的几何数据存在之前就排好版(尺寸只由计数决定)、
映射好,然后把可写窗口交给生产者。生产者那一遍写入落地的瞬间,字节就已经是 Painter 要打开的
那个文件了。

## 拷贝次数(诚实版)

| 段 | 拷贝 | 说明 |
|---|---|---|
| Blender 网格 → 竞技场 | **1 次** | `numpy.take(值, 顺序, out=映射页)`,直接 gather 进页面 |
| 竞技场 → Painter 导入器 | **0 次** | Painter 打开的路径就是那批页,只是一次页查找 |
| shader 参数,双向 | **0 次** | 直接读写控制块的内联区,两侧都已映射,不碰文件系统 |
| Painter 通道 → 竞技场 | 1 次 | Painter 的导出器自己写,写进的是页缓存 |
| 竞技场 → Blender 图像 | 1 次 | bpy 没有借指针入口,`Image.pixels` 是 Blender 自己的数组 |

**为什么贴图腿到不了零拷贝**(查证过,不是没试):Painter 既没有 C++/原生插件 SDK,Python API
也没有任何缓冲区形态的纹理入口或出口 —— Adobe 自己 2018 年那篇把 GL 纹理倒进共享内存的 R&D
改的是**引擎本体**,是 Labs 原型,从未作为公开 API 发布。Blender 这侧 5.2 确有
`imbuf.load_from_buffer` / `ImBuf.with_buffer`,但 `bpy.types.Image` **不接受 ImBuf**,走它反而
多一次拷贝。所以地板就是:Painter 往共享页写一次,Blender 从那页读一次。**不额外落地** ——
图像直接指向竞技场,靠保留策略保证它指着的那一代不会被抽掉。

去重不物化它比较的值:两个角只要顶点索引相同,位置就必然相同,所以键 = 顶点索引 + 角域属性
的原始 32 位字。点域数据完全不进键(顶点索引已经蕴含它),写的时候把两个索引数组复合起来,
所以它也只有一次 gather。

## 动作:送去 Cascadeur,做完拿回来

Blender 的动画确实不好用,这条腿就是补它的。面板上 **Animation** 那一段三个按钮:

1. **Send Rig and Animation to Cascadeur** —— 把选中的绑定和它当前的动作一起发过去,
   然后把 Cascadeur 叫起来。**骨架跟着表演一起走**:没有骨架的表演不是表演。
2. 在 Cascadeur 里做动作。
3. **Fetch Animation from Cascadeur** —— 要一份回来。回来的东西**按骨名落到你已有的那副绑定上**,
   临时导入的骨架当场删掉。所以来回多少次,你的骨轴都不会被转歪一点点。

Cascadeur **不驻留**:它跑一条命令就退出,所以桥是"召唤一次、造访一次"——
不是缺陷,是这个应用本来的样子,花名册里写着 `resident=False`,别处一行分支都没有。

实测(真装机,一次完整往返):

```
Blender 发   anim@Blender     4 node / 1 skin / 1 animation
Cascadeur    收下、做动作、导出并退出
Blender 收   matched=['Root','Wave']  missing=[]   30 条曲线落到已有绑定上
             objects left behind: ['RuriRig']      ← 临时导入物全部清掉
```

**只有 GLB,没有 FBX。** Cascadeur 自带 `csc.glb.process_import` / `process_export`
两个方向的一等门,所以中间不需要第二种格式 —— 第二种格式意味着同一份模型存在两份编码、
两套舍入。

## 装进三个宿主

Blender 侧不用装 —— 检出就在 addons 里。另外两个各走各的路,一条命令都办了:

```bash
python -m RuriBridge.Kernel.cli install ^
    --painter-plugins "<Painter 用户 python>/plugins" --enable-painter-plugin ^
    --cascadeur "<Cascadeur>/cascadeur.exe"
```

安装器不认识"哪个应用要怎么装" —— 它走一遍花名册,每一行自己说:检出本体(Blender)、
目录联接(Painter)、往它自己的命令目录写一扇门(Cascadeur)。加第四个应用是加一行,不是加分支。

建的是目录联接(`mklink /J`,不需要管理员)。`--enable-painter-plugin` 直接把
`launch_at_start` 写进 QSettings(Windows 上就是注册表),**Painter 必须关着**——它在启动时读。

- Blender:偏好设置 → 插件 → 启用 **RuriBridge**,面板在 3D 视图 N 面板 `RuriBridge` 页。
- Painter:插件随应用启动,面板是一个停靠窗口,启动时自动挂到默认会话。

两侧必须用同一个会话名(默认 `default`,Painter 侧可用环境变量 `RURI_BRIDGE_SESSION` 覆盖)。
会话根默认 `%LOCALAPPDATA%\RuriDccBridge\sessions`,可用 `RURI_BRIDGE_ROOT` 覆盖。

**不放临时目录**,尽管文件都带 `FILE_ATTRIBUTE_TEMPORARY` —— 这两件事无关:属性是求缓存管理器
把页留在内存,目录决定谁有权删它。消费者的图像指着这里的载荷,不能让一次磁盘清理把它抹掉。

**保留策略**:回收时保住两代 —— 最新的,和消费者**最后确认的那一代**。确认的含义是「我取到了」,
不是「我用完了」:Blender 的图像会一直指着它,直到更新的一代把它替换掉。两代就是全部代价。

`--copy` 也支持,但复制单个宿主目录会找不到上层的共享核心 —— 要复制就整仓复制。

## 命令行

```bash
python -m RuriBridge.Kernel.cli status                 # 控制块:每个通道的代次/确认/丢弃/载荷字节
python -m RuriBridge.Kernel.cli inspect --channel mesh@Blender
python -m RuriBridge.Kernel.cli verify-mesh            # 重读发布的 GLB 并逐项判定
python -m RuriBridge.Kernel.cli textures --into <目录>  # 列出 Painter 最新一批贴图,可另存
python -m RuriBridge.Kernel.cli values                 # 内联状态槽:双向的 shader 值,不落任何文件
python -m RuriBridge.Kernel.cli shaders --name intensity
python -m RuriBridge.Kernel.cli sessions / remove
python -m RuriBridge.Kernel.cli publish-mesh   --blender <blender.exe> --blend <场景> --scope VISIBLE
python -m RuriBridge.Kernel.cli request-export --blender <blender.exe> --blend <场景>
python -m RuriBridge.Kernel.cli pull-textures  --blender <blender.exe> --blend <场景> --bind --save
```

`pull-textures` 默认取**最新一批**(不管它是不是在本进程附着之前发布的);`--wait` 才是等新的。
两者是两个不同的问题:插件附着时故意不重放历史(否则打开 Blender 就会莫名其妙吞下一小时前的贴图),
而"把 Painter 现在有的拿过来"是显式动作,面板上是 Pull Latest Textures 按钮。

三个驱动动作都是**让 Blender 去发**,而不是命令行自己发:每条通道按设计只有一个写者,
命令行悄悄变成第二个写者就会和插件抢代次号。

`verify-mesh` 是真判据,不是打印:它重新读回发布的 GLB,核对文件头声明的字节数与实际大小、
每个 bufferView 落在二进制块内、每个索引落在自己的顶点数内、声明的包围盒真的包住位置。

## 身份就是名字,只是一个不再动的名字

**跨桥的材质名是「Painter 认得的那个名字」。** 第一次发布时,材质**当时的名字**被抄进
`ruri_bridge_identity`(自定义属性)并从此不动,glTF 里的材质名放的就是它。

为什么不能直接用当前名字:Painter 是**按网格里的材质名**把纹理集认回去的。你在 Blender 里改
一次材质名,下次 reload_mesh 时 Painter 就会认为那是一个**新材质**、建一个新纹理集,
**你画的东西留在旧的那个上,等于丢了**。抄一份不动的名字发过去,这件事就不可能发生。

为什么不能用 uuid:`original_name` 是 Painter **纹理集列表里显示的东西**。发 uuid 过去,
那一列就是一堆十六进制,谁也认不出哪个是脸哪个是头发。身份必须同时满足两件事 ——
不随改名移动,并且**是个人能读的名字**。

- 在 Blender 改材质名 → 身份不变 → Painter 那边纹理集原地不动,显示名跟着改成新名字。
- 在 Painter 改纹理集名 → `original_name`(= 身份)不受 setter 影响 → 回程照样认得。
- 图像数据块也按身份索引(`<身份>/<通道>`),所以图像名也是可读的。
- 两个材质抢同一个身份(把 A 改名成 B、再新建一个叫 A 的),发布时当场拆开,
  按 Blender 自己的规矩加 `.001`,并在日志里说清楚是谁跟谁。

**场景的身份是个例外,它是 uuid** —— 它只活在 Painter 工程的元数据里,没人看得见,而且要回答
「这个工程属不属于这个场景」;两个都叫 Scene 的文件是常态,拿名字回答会把它们错绑在一起。

### 身份存在 .blend 里,所以**必须存盘**

身份是自定义属性,住在 .blend 里。**没存盘的文件下次打开就没有身份**,发布时会当场铸一批新的,
Painter 按新身份认不出老纹理集,于是在画好的那些旁边**再建一批空的**。

这件事不再是静默的,两边都会说:

- Blender:`21 material(s) had no identity until now; save this file ...`(算子警告 + 日志)。
- Painter:`N Texture Set(s) keep their paint but have no material in this send ...`,
  并把前几个名字列出来。

刚铸出来的身份**不会**被当成"另一个场景"而拒绝 —— 发的那边自己都没记忆,拒绝没有意义;
两边都有持久身份而对不上,才是真冲突,那时才拒绝。

### 没有身份时按同名接上(手工做的工程也能接)

**这是默认行为,不用配置。** 一个手工做的 .spp、或者一个从没存过盘的场景,两边都没有身份可对。
这时:

1. Painter 发现**送来的材质没有一个对得上现有纹理集**,而且发的那边自明「我没有记忆」,
   就不 reload,而是先把自己的纹理集清单发回去,再请 Blender **重发一次**。
2. Blender 收到之后,把没有身份的材质按**同名**认领 Painter 那边纹理集的名字当身份,再发。
3. Painter 这次对得上,直接 reload 进那些**已经画好的**纹理集。

判据是**顶点数**:工程记得自己是从多少个顶点建起来的(存在元数据里),对不上就是另一个模型,
那就不按名字认领。手工做的工程从来没记过这个数,那时候名字就是两边唯一都有的东西 ——
而这正是这条路要服务的场景。整个握手每个工程只做一次。

## 关掉再打开:工程和贴图都还在

**Painter 工程认得它画的是哪个场景,靠的是存在工程文件里的数据。** 场景在第一次发布时
也拿到一个 `ruri_bridge_identity`,随网格一起发过去;Painter 把它写进
`project.Metadata("RuriBridge")` —— 那是 Painter 自己的工程元数据,**存盘会带着**,
下次打开还在。所以"这个工程画的是不是这个场景"是**数据回答的**,不是靠名字或路径猜的。

工程文件的位置写在 Blender 面板的 **Project** 一栏,存在场景里(所以随 .blend 存盘):

- 填了但文件还不在 → Painter 建完工程**就存到那儿**,不用你记得按 Ctrl+S。
- 填了而且文件在 → 冷启动时**打开那个工程再 reload 网格**(保笔触),而不是新建一个把
  画好的东西晾在硬盘上。
- 在 Painter 里手动另存 → `ProjectSaved` 事件把新路径发回来,场景那一栏跟着更新。
- 已经开着的工程属于**另一个**场景 → 拒绝,并把两个身份都说出来。刚铸出来的身份不算数
  (说明发的那边还没存过盘,它自己也没记忆),这种情况是认领而不是拒绝。

**没存盘的工程绝不会被悄悄关掉**:显式选 Create Project 而当前工程有未保存改动时,
桥会拒绝并让你先存。

**贴图跟着 .blend 走。** 摄取来的图像直接读竞技场那份页,活着的时候一次不拷;但竞技场
只留最新的两代,存了盘的 .blend 压在旧代上,明天打开就会**满地缺图**。所以存盘那一刻
把桥来的图像 pack 进 .blend(**Keep Textures In File**,默认开)—— 需要持久化的时候才付
这个代价,拿的也是 Blender 自己的机制。新一批贴图到货时会先把旧的 pack 丢掉,免得旧数据
盖住新页。

## 材质是回程的落点,没有就建一个

回程要落地,得有一个身份对得上的 Blender 材质。所以发布时会检查:**任何没有材质槽、或者槽里
是空的对象,当场给它建一个材质**(按对象名命名),并在日志和状态栏里说出来。

这不是多事:之前没有这一步时,一个没有材质的对象会让桥拿对象名当材质名发过去,回来时
Blender 里根本没有那个材质 —— 贴图**确实到了**(图像数据块都在),但没有任何材质在用它,
于是"画了没反应"。那是静默错,不是用户忘了。

回程落地时,材质里已有的、按通道名打了标签的图像纹理节点优先;**没有的就现建一个**,并接到
名字对得上的着色器输入上(比较时忽略大小写与分隔符,所以生成的节点组只要输入名按通道命名
就自动接上)。接哪个走**输入的类型**判断:向量输入自动插一个法线贴图节点。对不上的通道也会
建出带标签的节点,只是不接线 —— 到货了,等你用,而不是消失。

## 两侧面板是对称的

| 动作 | Blender | Painter |
|---|---|---|
| 发几何 | Send To Painter | Ask Blender For The Scene(请对面发) |
| 发贴图 | Ask For Textures(请对面发) | Send Textures To Blender |
| 发参数 | Send Shader Values | Send Shader Values To Blender |
| 取贴图 | Pull Latest Textures | —(Painter 不吃贴图) |
| 实时开关 | Live Sync + Shader Values / Mesh | Live sync + Textures / Shader Values |
| 在场 | Painter is attached | Blender is attached |

两边都能主动发起,所以你在哪边都不用切窗口。


## 通道与记录

通道名**不写在任何地方**,是算出来的:`<主题>@<说话的人>`,由「有哪些主题」乘以
「谁能说」得出。一条通道只有一个写者,所以除了槽本身的 seqlock 之外没有任何锁。

| 主题 | 谁能说 | 谁能听 | 语义 |
|---|---|---|---|
| `mesh` | Blender | Painter | 队列 |
| `tex` | Painter | Blender | 队列 |
| `anim` | Blender、Cascadeur | Blender、Cascadeur | 队列 |
| `shade` | Blender、Painter | Blender、Painter | 状态 |
| `here` | 三个都能 | 三个都能 | 状态 |
| `ask` | 三个都能 | 三个都能 | 队列 |

**谁能说谁能听不是表,是能力的连接。** 主题问一条能力,应用答一条能力,剩下的自己长出来:
Painter 没有动画面,所以它永远收不到 `anim`;Cascadeur 不是模型的作者,所以它永远发不出 `mesh`。
加第四个应用只是花名册里多一行,通道随之出现。

**队列**是不可变 generation 目录,未确认的不丢,顺序有意义;**状态**是一个槽,后写覆盖前写 ——
一个已被取代的值没有任何意义,为它造目录和文件是错的形状。

确认位**按听众分开**:一条通道现在可能有两个听众(`mesh@Blender` 谁都能听),共用一个确认位
会让先读的那个把载荷从后读的那个眼皮底下回收掉。每个应用在自己的那一格里确认,发布者回收时
取「所有真正在听的人」的最小值。

`mesh` 记录里的 `materials` 是**原样搬运**的:名字 + Blender 材质的自定义属性 + 用到的节点组名。
桥不解释它们 —— 着色器生成器改词汇,这里一行都不用动。

## 回到 Blender 之后怎么接上

Painter 的纹理集是照 glTF 材质命名的,而那个名字就是本桥发过去的 Blender 材质名 —— 所以目标材质
没有歧义。材质内部按**图像纹理节点的标签**认领通道,比较时忽略大小写与分隔符(`BaseColor` 认得
Painter 的 `Base_color`),两个通道归一化后同名则直接报错,不猜。

摄取进来的图一律挂 fake user:还没接线的通道用户数为零,而 Blender **不把零用户数据块写进文件** ——
不挂就会出现"拉了贴图、存盘、重开,没接线的那几张不见了"。

## 色彩空间

色彩空间从不猜。它由知道通道格式的那一侧写进记录,消费侧原样套用。

判据只有一个输入:`ChannelFormat` 的存储列。**`sRGB8` 是唯一以 sRGB 编码存储的成员**,其余全部
线性存储 —— 所以只有一个答案是"编码",其余都是"按原值用"(Blender 的 `Non-Color`)。

`Channel.is_color()` **故意不参与判定**。它的含义是「RGB 还是灰度」,不是「是不是感知色彩」——
法线通道是 RGB16F,`is_color()` 为真。让它去区分"线性色彩"和"数据",会把每个默认 Painter
工程里的每张法线图都标成色彩;在 Blender 默认场景线性空间下这不花钱,换 ACES 配置就会把法线
过一遍基色转换。

代价说清楚:如果谁把 BaseColor 设成 RGB32F(线性 HDR 色彩),它会被标成 `Non-Color`。默认配置下
与 `Linear Rec.709` 数值等价,非默认色彩管理下需要手动改。用格式单独判,这两种情况本来就区分不了。

## 实时同步(默认开,可关)

三条腿都是**变更驱动**的,不是请求驱动:

| 腿 | 怎么察觉 | 静默期 | 实测代价 |
|---|---|---|---|
| Painter 画笔 → Blender | `TextureStateEvent`(Painter 唯一的落笔信号) | 0.9s | **只导脏通道**,157KB 对全量 545KB |
| shader 参数,双向 | 各自轮询自己的值比对指纹 | 0.35s | 出价 **3.6ms 采集 + 9.3ms 推送**;Painter 侧按实测耗时自定节奏 |
| Blender 几何 → Painter | `depsgraph_update_post` 只数 `is_updated_geometry` | 1.2s | 一次整网格重载,所以按「离开编辑模式」的节奏走 |

真角色上的实测(JsspSi:17 个物体 / 43803 面 / 16 个材质 / 5.1MB GLB):一次活同步
**41ms**(什么都没改)到 **82ms**(最重的那个物体动了,28799 三角),整份发送 725ms,
动画 3.7s。泵 60ms 一拍,空转一拍 0.05ms。

关掉:Blender N 面板的 **Live Sync**(以及分开的 Shader Values / Mesh),Painter 停靠面板的
**Live sync to Blender**。默认全开。

作用域默认是**整个场景的可见 mesh**,而且**每次都重新解析** —— 后来新建的对象自动进同步,
不用重发。要只发选中的,把作用域改成 Selected。

(选择是通过视图层的 `select_get()` 读的,不是 `context.selected_objects` —— 后者在定时器里
读不到,而实测前者在真 timer 回调里可用,这样按钮和活同步问的是同一个问题。)

**三件事必须一起做对,否则不是活同步而是灾难**,`ruri_bridge/sync.py` 的 `ChangeGate` 一次解决:

1. **察觉**:宿主不会为桥关心的一切发事件,所以要么用它确实发的事件,要么在定时器上比对便宜的指纹。
2. **收敛**:一笔、一次拖动、一次拽点产生的是**一串**变更。逐事件发布会送出几百代,网格腿还会让
   对面在拖动中途重载。所以指纹**停下来**并过了静默期才发。
3. **不回声**:两边都发布自己观察到的状态,值就会永远弹来弹去。解法不是定时器也不是会衰减的标志位,
   是**记住从对面来的那个指纹并拒绝发布它**。

首次连接两侧都只「认领现状」不发布 —— 否则新建工程的缺省值会盖掉你在 .blend 里写好的值。

Painter 应用完之后,只要有名字没被采纳(未知/类型不符/冲突),它会把**实际生效的值写回来**,
让冲突在 Blender 里现形,而不是在 .blend 里留一个假值。


## 视口着色器参数

`substance_painter` 包里**没有 `shaders` 模块** —— 着色器实例、参数、以及哪个纹理集跑哪个实例,
只活在 `alg.shaders`。但 `substance_painter.js.evaluate` 本身就是 Python 入口,而且它**内部已经
`json.loads`**,回来的是解析好的 Python 对象(类型标注写着 `-> str`,是过期的)。所以这条腿仍然
是 Python 调 Painter。引擎是旧版,片段一律 ES5。

Blender 侧**不按名字过滤**:它不可能知道对面的 shader 暴露了哪些 uniform,在这边维护一张名字表
就是给一件能直接问出来的事造第二个真源。所以整行数据发过去,交集在 Painter 侧对着
`alg.shaders.parameters` 算,算不上的**报回来**(`unknown` / `mismatched` / `unmapped`),
不静默丢弃。类型按 `dataType` 的名字尾数推 arity(`Float3` → 3),所以出现 `Float4` 不用改代码。

**共用实例是真陷阱**:默认工程里所有纹理集共用同一个「主要着色器」实例,所以针对两个材质的推送
落在同一批 uniform 上。两边对同一个参数给了不同的值时,**两个都不写**,记进 `conflicting` ——
静默取一个会让视口显示一个谁都没要求过的数。

**生成的着色器会自动换上**。着色器生成器给材质烙了它是为哪个着色器写的,所以一份出价落在跑着
别的着色器的纹理集上时,桥不是报「这 136 个名字都不认识」,而是去货架上找那个着色器,给**每个
纹理集各建一个自己的实例**再换上去(共用实例装不下十六份不同的值)。货架里还没有?出价留着,
等 Painter 把货架收拾完再上 —— 启动那一拍它的资源索引常常还没建好。装着色器不是桥的事:
生成器写它,导入器把它放进货架。

值真的到了没有,判据是**两边逐个对**:JsspSi 上 16 个纹理集共享 1022 个参数,1022 个相等。

```bash
python -m RuriBridge.Kernel.cli shaders --name intensity          # Painter 现在暴露什么、值多少
python -m RuriBridge.Kernel.cli push-shader-values --blender <blender.exe> --blend <场景>
```

## 已知边界

- 导出预设名按**实测目录**取(`Document channels + Normal + AO (No Alpha)`)。名字给错时错误信息
  会把 Painter 当前提供的全部预设列出来 —— 这条错误信息本身就是这个默认值第一次被改对的原因。
- UDIM 摄取按 Blender 的 `<UDIM>` 平铺图实现了,但没有 UDIM 工程可跑,未实测。
- Windows only,这是机制本身决定的(Win32 section 语义),不是没写别的分支。
