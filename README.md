# RuriDccBridge

Blender 5.x ⇄ Adobe Substance 3D Painter 的共享内存桥。网格从 Blender 直接写进
Painter 会映射的**同一批物理页**,通道贴图从 Painter 原样回到 Blender。

一个仓库,两个宿主入口,一份共享核心 —— 用目录联接挂进两个宿主,不复制、不分叉。

```
RuriDccBridge/
  ruri_bridge/       共享核心。不 import bpy,不 import substance_painter,纯标准库
    arena.py         会话竞技场:控制块(seqlock 槽) + 不可变 generation 目录
    channel.py       单写者发布/订阅,append-only,未确认的不丢
    record.py        线格式:通道、记录种类、色彩空间判据
    glb.py           就地 GLB:先按计数排好版再映射,生产者一次写入即成品
    cli.py           无宿主的命令行:观测、验证、驱动
  blender_addon/     Blender 插件(需要 numpy,Blender 自带)
  painter_plugin/    Painter 插件(纯标准库 + PySide6 + substance_painter)
```

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
| Painter 通道 → 竞技场 | **1 次** | Painter 的导出器自己写文件,写进的是页缓存 |
| 竞技场 → Blender 图像 | **1 次** | bpy 没有借指针入口,`Image.pixels` 是 Blender 自己的 float 数组 |

去重不物化它比较的值:两个角只要顶点索引相同,位置就必然相同,所以键 = 顶点索引 + 角域属性
的原始 32 位字。点域数据完全不进键(顶点索引已经蕴含它),写的时候把两个索引数组复合起来,
所以它也只有一次 gather。

## 装进两个宿主

```bash
python -m ruri_bridge.cli install --blender-addons "<Blender 用户 scripts>/addons" --painter-plugins "<Painter 用户 python>/plugins"
```

建的是目录联接(`mklink /J`,不需要管理员)。之后:

- Blender:偏好设置 → 插件 → 启用 **RuriBridge**,面板在 3D 视图 N 面板 `RuriBridge` 页。
- Painter:Python 菜单里勾上 **RuriBridge**(`plugins/` 下的插件是可选组件,勾一次就记住),
  面板是一个停靠窗口。插件启动时会自动挂到默认会话。

两侧必须用同一个会话名(默认 `default`,Painter 侧可用环境变量 `RURI_BRIDGE_SESSION` 覆盖)。
会话根默认 `%TEMP%\RuriDccBridge`,可用 `RURI_BRIDGE_ROOT` 覆盖。

`--copy` 也支持,但复制单个宿主目录会找不到上层的共享核心 —— 要复制就整仓复制。

## 命令行

```bash
python -m ruri_bridge.cli status                 # 控制块:每个通道的代次/确认/丢弃/载荷字节
python -m ruri_bridge.cli inspect --channel to_painter
python -m ruri_bridge.cli verify-mesh            # 重读发布的 GLB 并逐项判定
python -m ruri_bridge.cli textures --into <目录>  # 列出 Painter 最新一批贴图,可另存
python -m ruri_bridge.cli sessions / remove
python -m ruri_bridge.cli publish-mesh   --blender <blender.exe> --blend <场景> --scope VISIBLE
python -m ruri_bridge.cli request-export --blender <blender.exe> --blend <场景>
python -m ruri_bridge.cli pull-textures  --blender <blender.exe> --blend <场景> --bind --save
```

`pull-textures` 默认取**最新一批**(不管它是不是在本进程附着之前发布的);`--wait` 才是等新的。
两者是两个不同的问题:插件附着时故意不重放历史(否则打开 Blender 就会莫名其妙吞下一小时前的贴图),
而"把 Painter 现在有的拿过来"是显式动作,面板上是 Pull Latest Textures 按钮。

三个驱动动作都是**让 Blender 去发**,而不是命令行自己发:`to_painter` 按设计只有一个写者,
命令行悄悄变成第二个写者就会和插件抢代次号。

`verify-mesh` 是真判据,不是打印:它重新读回发布的 GLB,核对文件头声明的字节数与实际大小、
每个 bufferView 落在二进制块内、每个索引落在自己的顶点数内、声明的包围盒真的包住位置。

## 通道与记录

两个通道,各只有一个写者,所以除了槽本身的 seqlock 之外没有任何锁。

| 通道 | 写者 | 记录种类 |
|---|---|---|
| `to_painter` | Blender | `mesh`(GLB + 材质数据行 + 意图)、`export_request` |
| `to_blender` | Painter | `textures`(每张图的位深/格式/色彩空间)、`project_state` |

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

## 已知边界

- **Painter 的 Python API 没有 shader 参数面**。`substance_painter` 包里没有 `shaders` 模块,
  参数只活在 JS 的 `alg.shaders`。所以桥不往 Painter 的视口 shader 里推 uniform,只把材质数据行
  当数据搬过去。
- 导出预设名按**实测目录**取(`Document channels + Normal + AO (No Alpha)`)。名字给错时错误信息
  会把 Painter 当前提供的全部预设列出来 —— 这条错误信息本身就是这个默认值第一次被改对的原因。
- UDIM 摄取按 Blender 的 `<UDIM>` 平铺图实现了,但没有 UDIM 工程可跑,未实测。
- Windows only,这是机制本身决定的(Win32 section 语义),不是没写别的分支。
