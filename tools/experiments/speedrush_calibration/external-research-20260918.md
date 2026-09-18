> **本文是什么**：外部 LLM 调查报告（2026-09-18，维护者委托、粘贴归档），主题为
> 「固定追车相机下像素→地面标定」的方法族调查。**未经本项目逐条验证**：方法主干已采纳
> （见 [`README.md`](README.md)），但其中把真实道路先验（车道宽 3.5 米、静止目标）当约束的
> 三处已在本项目侧更正——**读这份报告必须先读 README 的更正清单**。
> 文中引用链接为报告自带，**未逐条核链**；其结论按 L4 待验证材料采信。
> **本文不是什么**：不是本项目的计划，不是规则真源。

可以，而且从你给出的约束看，**这不是一个需要“做视觉里程计/深度估计”的问题**。它更接近：

> **固定相机 + 近似平面道路 + 已知道路横向尺度 + 单一主运动方向的单目平面测量。**

我查了一轮经典单视图测量、道路/车道消失点标定、IPM、车道拟合和车辆速度辅助标定资料后，结论比较明确：

**第一版应该坚定地走“静态几何自标定 → 平面逆投影”，把动态信息放到验证层，而不是反过来用目标运动拟合相机参数。**

尤其是你第一次的失败，其实很好地说明了这一点：运动学拟合把**未知目标运动**和**未知投影参数**绑在了一起，属于不可控的联合误差源；而车道线/护栏本身就是你要估计的投影几何的直接观测。

---

# 1. 先给结论

推荐把问题压缩成下面这个模型：

$$
(u,v)\rightarrow(X,Z)
$$

其中：

* \(X\)：相对自车中心的横向距离，单位 m
* \(Z\)：前方距离，单位 m
* \(u,v\)：目标接地点像素

第一版只估计：

$$
\theta =
(c_x,\; y_h,\; H,\; f)
$$

如果实际相机水平位置已由“自车始终居中”确认，则进一步固定：

$$
c_x=640
$$

于是只有 **3 个核心自由度**：

$$
(y_h,\;H,\;f)
$$

或者不显式解释物理意义，直接使用：

$$
(y_h,\; A_x,\; A_z)
$$

做工程标定。

在理想的水平道路、零 roll、光轴与车辆纵轴对齐条件下，可以写成近似形式：

$$
Z = \frac{A_z}{y_h-v}
$$

$$
X = A_x\frac{u-c_x}{y_h-v}
$$

这恰好解释了为什么你预期的 **4～6 个自由度**是合理的。

但是这里有一个非常重要的识别问题：

> **VP/horizon 本身不能给你绝对米制尺度。**

经典 single-view metrology 也明确指出，仅凭参考平面消失线和方向消失点可以恢复大量几何关系，但长度/距离存在共同尺度，需要额外的已知尺度约束。([Microsoft][1])

所以你的“车道真实宽度”是非常有价值的标尺。

---

# 2. A：静态车道线 → 消失点

这是我认为**最应该作为主方案**的部分。

## 原理

道路上近似平行的车道线在透视投影下汇聚到共同消失点：

$$
VP=(x_{vp},y_h)
$$

因此可以从大量车道线候选段中寻找共同交点。

这不是一个为了这个项目临时拼出来的方法。经典道路跟踪工作已经直接把 road boundaries 的消失点作为道路方向信息；后续车道检测工作也大量采用 Hough/LSD + VP 的路线。([科学直通车][2])

OpenCV 本身也提供标准 Hough 和 probabilistic Hough：

* `HoughLines`
* `HoughLinesP`

后者直接输出线段端点，并允许通过 `minLineLength` / `maxLineGap` 控制短线和断裂线段。([OpenCV文档][3])

### 对你的场景怎么改

**不要做“检测到两条最明显车道线 → 求交点”。**

那会被你明确指出的三个东西轻易干掉：

1. 虚线断裂
2. 对向车道
3. 弯道

应该做：

### Step A1：产生候选线段

ROI 只取道路区域，例如：

$$
v>v_{ROI}
$$

然后：

* Canny / 边缘
* HoughLinesP 或 LSD
* 保留足够长的线段

HoughP 对你的虚线反而是合适的，因为 `maxLineGap` 可以把小断裂视作同一候选结构。([OpenCV文档][3])

---

### Step A2：不是“找两条线”，而是“找一簇共点线”

对于每条候选线：

$$
l_i=(a_i,b_i,c_i)
$$

计算它与其他候选线的交点。

然后在 VP 空间进行投票。

也就是说：

> **VP 是统计量，不是某两条线的交点。**

这对你的场景非常重要。

因为：

* 左车道线贡献一次
* 右车道线贡献一次
* 护栏可能贡献一次
* 对向车道可能贡献错误簇
* 弯道线可能贡献局部错误簇

最终寻找：

$$
VP^*=\arg\max_V
\sum_i w_i\,\rho(d(V,l_i))
$$

而不是：

$$
VP=l_1\cap l_2
$$

---

# 3. 对“对向车道干扰”的处理

这是静态方案最大的实际问题之一。

但不需要神经网络。

你有一个极强的先验：

> **你的目标 VP 应该接近车辆前进方向，而且 \(x_{vp}\) 应该接近画面中心。**

因此可以对线段加权：

$$
w_i =
w_{\text{length}}
w_{\text{ROI}}
w_{\text{direction}}
w_{\text{temporal}}
$$

尤其：

### ① 距离图像底部越近，权重越高

因为这里的道路结构最可靠。

### ② 与预计前进方向夹角不合理的线段降权

第一轮甚至可以粗暴限定：

$$
x_{vp}\in[640-\Delta,640+\Delta]
$$

而不是让 VP 在整张无限图像里乱跑。

### ③ 时间一致性

这是你的 21 fps 视频特别适合做的：

$$
VP_t\approx VP_{t-1}
$$

如果某一帧突然从：

$$
(638,310)\rightarrow(512,180)
$$

然后下一帧又回来：

$$
(640,313)
$$

那不是相机真的突然动了，而是检测失败。

---

# 4. 弯道不能硬塞进“直线 VP”

这是第一个需要明确否定的地方。

**全局单应 + 单一 VP 并不能正确描述明显弯道。**

已有车道检测研究通常会在 IPM 后继续做二次/三次曲线拟合；例如有工作明确采用 IPM + RANSAC parabola fitting 来处理 straight/curve 场景。([PubMed Central (PMC)][4])

也有专门针对弯道的 straight-curve model，把直线段和多项式曲线段分开建模。([TRID][5])

所以：

> **弯道不是标定数据，而是标定后的运行区间判定问题。**

第一版标定只需要从**平直路段**获得参数。

之后：

* 平直路：正常 IPM
* 缓弯：可以继续使用局部车道曲线
* 大弯 / 跳台 / 坡道：标记 calibration invalid

不要试图让一个全局 homography 同时解释：

> 平路 + 坡道 + 桥 + 大弯。

这是模型结构本身不成立。

---

# 5. B：你的“\(y_h\)+尺度”模型是可行的，但有一个容易踩的坑

你提出：

> “车道线像素斜率收敛于 \((VP_x,y_h)\)”
> +
> “车道宽在画面底部的像素宽”

这个方向对，但要区分：

### 消失点解决的是几何形状

### 已知车道宽解决的是 metric scale

而不是：

> “VP + lane width 自动把所有参数都解出来”。

经典 single-view metrology 的结论正是这个方向：已知参考平面上的消失线/消失点，可以恢复平面中的 affine/projective 几何，但 metric scale 需要额外参考信息。([Microsoft][1])

道路场景的实际 camera calibration 文献也明确采用：

> lane vanishing point + lane width / 其他先验尺度

来求相机参数。([科学直通车][6])

---

# 6. 更推荐直接拟合“物理模型”，而不是分别拟合 y_h 和尺度

如果假设：

* roll ≈ 0
* yaw ≈ 0
* \(c_x\approx640\)
* 路面水平

那么可以把目标点的**接地点** \((u_i,v_i)\) 写成：

$$
X_i = H\frac{u_i-c_x}{y_h-v_i}
$$

$$
Z_i = \frac{fH}{y_h-v_i}
$$

因此只需要：

$$
\boxed{y_h,\ H,\ f}
$$

三个未知量。

如果 \(c_x\) 不可信：

$$
\boxed{c_x,\ y_h,\ H,\ f}
$$

四个未知量。

这比维护一个“万能 homography 8 参数矩阵”更适合你的项目，因为：

1. 参数少
2. 有明确物理含义
3. 跨 session 可以检查参数稳定性
4. 可以直接诊断“是 horizon 错了还是尺度错了”
5. 后续 pitch 漂移也能对应到 \(y_h\)

---

# 7. 一个非常关键的事实：车道宽约束其实可以大量消掉参数

假设同一横向位置、同一深度处，两条车道线真实间距：

$$
W
$$

那么它们投影后的横向距离：

$$
\Delta u
$$

满足：

$$
W
=
H
\frac{\Delta u}{y_h-v}
$$

因此：

$$
\boxed{
H=
W\frac{y_h-v}{\Delta u}
}
$$

如果你在多个 \(v\) 位置测：

$$
(v_j,\Delta u_j)
$$

那么每个点都提供一个 \(H\) 估计。

于是可以直接做超定拟合：

$$
H^*=
\arg\min_H
\sum_j
\rho\left(
H\frac{\Delta u_j}{y_h-v_j}-W
\right)
$$

这比“只测画面最底部一个车道宽”稳得多。

---

# 8. 为什么我反而建议你把“车道宽”当主要尺度观测

因为它具有三个优点：

### ① 不依赖目标速度

这是你第一次运动学拟合最缺的东西。

### ② 不依赖目标类别

金币、街车都可能尺寸不同。

### ③ 是整个场景的重复结构

多条车道可以提供大量冗余观测。

而且已有道路摄像头自标定研究明确讨论过使用 lane width 等先验信息进行 camera calibration。([科学直通车][6])

---

# 9. C：IPM 是成熟路线，而且非常适合你

IPM 本质上就是：

$$
image\ plane
\rightarrow
ground\ plane
$$

即：

$$
(u,v)\rightarrow(X,Z)
$$

用一个 homography 表示。

OpenCV 的 `findHomography` 官方实现就是标准的平面间透视变换，并支持 RANSAC / LMEDS / RHO；文档也明确说明四组对应点即可求平面 homography，实际可以用更多点并进行鲁棒估计。([OpenCV文档][7])

车道检测领域也存在直接：

> IPM → edge detection → RANSAC parabola fitting

的成熟实现路线。([PubMed Central (PMC)][4])

所以 IPM 本身没有任何问题。

真正的问题是：

> **你的 IPM 参数从哪里来？**

---

# 10. 不建议你人工拿四个点做一次 homography

因为你明确要求：

> 固定相机、跨 session 自动复现。

那么手工四点：

```text
左下
右下
左上
右上
```

虽然能跑，但是把 calibration 变成：

> “某个人在某帧画了四个点”。

这与项目需求不匹配。

应该：

### 场景自标定

$$
lane\ lines
\rightarrow VP/horizon
$$

$$
lane\ width
\rightarrow metric\ scale
$$

$$
\{VP,H,f\}
\rightarrow H_{ground}
$$

---

# 11. 标定板替代方案：你实际上不需要标定板

这里 Single View Metrology 的结果非常适合你的场景。

Criminisi/Reid/Zisserman 的经典工作就是研究：

> 不知道相机内参、不知道相机外参，仅利用场景里的 vanishing line / vanishing point / 已知尺度做单视图测量。([Microsoft][1])

而道路场景刚好天然提供：

* 平行车道线
* 道路平面
* 已知车道宽
* 车辆纵向方向
* 连续帧

所以：

**不需要额外 calibration board。**

---

# 12. D：弯道怎么办？

这里我建议你把问题拆成两个完全不同的东西。

## Calibration

只用：

> 平直路段

求固定相机参数。

## Runtime geometry

对于当前帧：

### 平直路

$$
IPM(H)
$$

### 弯道

在 IPM 后拟合：

$$
X(Z)=aZ^3+bZ^2+cZ+d
$$

或者二次式：

$$
X(Z)=aZ^2+bZ+c
$$

已有工作明确使用 polynomial / parabola fitting 来表示曲线车道。([PubMed Central (PMC)][4])

---

# 13. 但“由曲率反推航向偏置”不要放进第一版

这是一个容易把项目复杂度再次拉爆的地方。

你当前真正需要的是：

> 目标在哪条车道 + 相对横向距离 + 前方距离。

不一定需要：

> 精确恢复道路 Frenet frame。

所以第一版只需要：

$$
X(Z)
$$

即可。

例如：

```text
车道中心线：
X = aZ² + bZ + c

目标：
(u,v) -> (X_target,Z_target)

横向偏移：
X_target - X_lane(Z_target)
```

如果以后驾驶控制需要：

* 道路切线
* 航向误差
* 曲率

再从：

$$
X(Z)
$$

求导：

$$
\frac{dX}{dZ}
$$

即可。

---

# 14. “当前是不是平路？”——我不建议第一版引入复杂坡度估计

这是我认为你的设计里非常值得**降级处理**的一项。

你只需要一个：

$$
flat\_confidence
$$

而不需要完整估计坡度。

最简单的判据就是：

### ① VP 的时间稳定性

平路：

$$
y_h(t)\approx const
$$

坡道：

$$
y_h(t)
$$

会系统性漂移。

### ② 多条平行道路边界是否共享同一 VP

如果左/右车道线：

$$
VP_1,VP_2,VP_3
$$

不再形成稳定簇，则：

```text
flat_confidence ↓
```

### ③ IPM 后车道宽是否随距离系统性异常

正常平面：

$$
W(Z)\approx W
$$

如果 IPM 后：

```text
近处 3.5m
中间 4.2m
远处 5.1m
```

说明模型不再解释当前表面。

这个判据甚至比直接“识别坡道”更符合你的目标。

有研究专门利用 VP 特征估计 road slope，也说明坡度会破坏普通平面假设。([科学直通车][8])

---

# 15. E：相机俯仰抖动——这里有一个非常漂亮的低成本解决方案

你现在不需要 IMU。

因为：

> pitch 变化 ≈ horizon \(y_h\) 变化。

已有车辆视觉研究直接用 vanishing point 的 y 坐标估计 pitch。([PubMed Central (PMC)][9])

而关于车载相机 pitch calibration 的工作也明确指出，车辆运动过程中车身振动会造成 camera pitch 改变，可以通过连续图像中的 VP 估计进行补偿。([谷歌专利][10])

因此：

每帧估计：

$$
VP_t=(x_t,y_t)
$$

然后：

$$
\Delta pitch_t
\propto
y_t-y_{h,0}
$$

你甚至不需要把它转成角度。

直接维护：

```text
horizon_y(t)
```

就够了。

---

# 16. 更推荐“静态线 → VP”的帧间滤波

不要：

```text
每帧重新标定
```

而是：

```text
offline calibration
        ↓
base_yh
        ↓
runtime 每帧估计 Δyh
        ↓
小范围修正
```

例如：

$$
y_h(t)
=
\operatorname{median}_{k=t-N}^{t}
y_h(k)
$$

再加一个变化率限制：

$$
|y_h(t)-y_h(t-1)|<\epsilon
$$

这样偶尔一帧车道检测错了，不会让整个坐标系跳掉。

---

# 17. F：HUD 速度可以用，但我强烈建议只当“独立校验”

你问：

> 已知自车速度序列，能不能用自车位移校验纵向尺度？

**可以，而且很有价值。**

但不要重新把它变成主标定器。

假设 HUD：

$$
v_t
$$

积分：

$$
S_t=\int v(t)dt
$$

那么连续帧的道路静态纹理应该满足：

$$
\Delta S_t
$$

与视觉上的地面运动量相一致。

这其实是一个简化版的 optical-flow / visual odometry 校验。

已知速度辅助从地面图像运动估计尺度，本身也有成熟先例；相关工作明确利用已知车辆速度与图像地面运动之间的比例关系修正相机高度/尺度。([谷歌专利][11])

但你的情况更简单：

> **不是用它解 calibration，而是用它验证 calibration。**

这是非常重要的架构区别。

---

# 18. 你的第一次运动学拟合为什么失败，其实可以正式定性

你的模型：

$$
\frac1{v-y_h}
=
\frac{t_{arrive}-t}{B}
$$

本质上在同时估计：

* \(y_h\)
* 到达时间
* 目标相对速度模型

但：

$$
v_{image}(t)
$$

不是只由相机投影决定。

它还由：

$$
Z_{relative}(t)
$$

决定。

而：

$$
Z_{relative}(t)
=
Z_{target}(t)-Z_{ego}(t)
$$

目标自身运动未知。

因此：

$$
\boxed{
geometry\ parameter
\leftrightarrow
object\ motion
}
$$

发生了耦合。

所以你得到：

$$
y_h=315
$$

并不奇怪。

这不是简单的“优化器没找到正确解”。

而是：

> **你的观测模型允许运动自由度伪装成透视参数。**

这正是静态几何标定应该优先解决的问题。

---

# 19. A/B/C/D/E/F 的最终取舍

| 方法                              | 第一版                 |
| ------------------------------- | ------------------- |
| Hough / LSD                     | **要**               |
| VP 投票                           | **要**               |
| 多帧 VP 中值/鲁棒估计                   | **要**               |
| lane width metric scale         | **要**               |
| IPM / ground homography         | **要**               |
| RANSAC                          | **要**               |
| 弯道 polynomial                   | **运行期可选**           |
| slope detector                  | **只做 invalid 判据**   |
| frame-to-frame pitch correction | **第二阶段**            |
| HUD speed                       | **独立验证**            |
| 目标运动学拟合                         | **排除出 calibration** |

---

# 20. 我推荐的最小可行标定器

我会直接把它定义成：

## 参数

第一版：

$$
\boxed{
\theta=(c_x,y_h,H,f)
}
$$

但固定：

$$
c_x=640
$$

于是：

$$
\boxed{
\theta=(y_h,H,f)
}
$$

如果你不想碰物理相机模型，也可以直接使用：

$$
\boxed{
(y_h,A_x,A_z)
}
$$

我更推荐前者，因为以后解释结果非常方便。

---

# 21. 输入

离线输入：

```text
frames/
detections/
lane_evidence/
hud_speed/
```

其中 lane evidence 不需要完整语义分割。

只需要类似：

```text
frame_id:
    line_segments:
        [(x1,y1,x2,y2), ...]
```

甚至人工/半自动标一小段视频都可以。

---

# 22. Step 1：选择“平直路段”

不要把全视频扔进去。

从视频中找：

* 车道线明显
* 连续路面
* 无明显坡道
* 无跳台
* 无桥面
* 弯曲程度低

例如选：

```text
30~60 秒
```

即可。

---

# 23. Step 2：每帧估计 VP

对每个 frame：

```text
line segments
      ↓
方向过滤
      ↓
pairwise intersections
      ↓
VP voting
      ↓
VP_t
```

然后：

$$
VP^*=
median(VP_t)
$$

或者更稳：

$$
VP^*=
RANSAC/Huber
$$

得到：

$$
(x_{vp},y_h)
$$

---

# 24. Step 3：检查 VP 是否真的可信

这里不要急着继续。

画出：

```text
所有候选线
+
VP
+
从 VP 发出的射线
```

人工看一次非常值得。

你应该看到：

```text
左车道线  \
左边线     \
             ● VP
右边线     /
车道线     /
```

而不是：

```text
        ●

  \     |    /
   \    |   /
----\---|--/---
```

如果 VP 被护栏/建筑吸走：

**实验到这里就应该判失败。**

---

# 25. Step 4：利用 lane width 求尺度

从多帧、多距离取：

$$
(v_j,\Delta u_j)
$$

已知：

$$
W_{lane}
$$

拟合：

$$
W_{lane}
\approx
H\frac{\Delta u_j}{y_h-v_j}
$$

使用 robust loss：

$$
H^*=
\arg\min
\sum_j
\rho(e_j)
$$

推荐：

* Huber
* RANSAC

而不是普通 least squares。

因为某些 \(\Delta u\) 很可能其实来自：

* 对向车道
* 护栏
* 错配车道线
* 弯道

---

# 26. Step 5：确定 forward scale

如果你知道游戏实际相机 FOV / focal length：

直接用。

如果不知道：

### 第一选择

从游戏静态镜头参数/渲染设置获得。

### 第二选择

通过一个已知纵向距离标记校准。

例如：

* 道路上两个已知距离点
* 已知长度的车道标线
* 游戏地图里已知物理间距

Single-view metrology 的理论允许利用已知参考长度恢复 metric scale。([Microsoft][1])

### 第三选择

用 HUD 位移做独立尺度拟合。

但这是 fallback，不应该成为第一选择。

---

# 27. Step 6：构造最终映射

得到：

$$
y_h,H,f,c_x
$$

之后：

$$
\boxed{
Z =
\frac{fH}{y_h-v}
}
$$

$$
\boxed{
X =
H\frac{u-c_x}{y_h-v}
}
$$

注意：

### 目标点不能用 bbox 中心

必须尽可能使用：

$$
(u,\ v_{bottom})
$$

即：

> **车辆/金币接触路面的估计点。**

否则一个高车框的中心和一个低矮金币的中心根本不是同一个世界平面点。

这会直接污染你后面的距离模型。

---

# 28. Step 7：车道号

得到：

$$
X
$$

之后：

$$
lane =
\operatorname{round}
\left(
\frac{X}{W_{lane}}
\right)
$$

但这里不要简单使用绝对 \(X/W\)。

更稳的是从车道中心：

$$
C_k(Z)
$$

计算：

$$
d_k=X-C_k(Z)
$$

选择：

$$
k^*=\arg\min_k |d_k|
$$

然后输出：

```text
lane = k*
lateral_offset = d_k
```

这样弯道情况下仍然可以工作。

---

# 29. 最关键的离线实验协议

我建议把实验设计成**四级闸门**。

---

## Gate 0：几何自检

输出：

```text
VP heatmap
VP_t trajectory
lane-line overlay
```

### 成功

例如：

```text
VPx = 638 ± 6 px
VPy = 308 ± 4 px
```

### 失败信号

```text
VPx 500~750 大范围跳
VPy 200~400 漂
```

或者不同道路边界形成两个稳定 VP。

这意味着：

> 不是优化器问题，而是“你提取的线不是同一个几何族”。

---

# 30. Gate 1：不变道不变性

这是你给出的第一个硬验收。

选择明确：

> **没有变道的目标轨迹。**

计算：

$$
X_t
$$

然后：

$$
median(X_t)
$$

和：

$$
MAD(X_t)
$$

重点看：

$$
median(|X_t-\tilde X|)
<
0.25W
$$

### 错误信号

如果目标实际不变道：

```text
X:
0.2
0.4
0.8
1.1
1.5
...
```

持续单向漂移：

**通常不是 detector jitter。**

更可能是：

* \(y_h\) 错
* pitch 在变化
* 目标接地点错
* 平面假设失效

---

# 31. Gate 2：车道号阶跃

收集：

$$
X_t
$$

画 histogram / KDE。

你希望看到：

```text
      /\       /\       /\
     /  \     /  \     /  \
----|----|---|----|---|----|---
    lane -1  lane 0   lane +1
```

而不是：

```text
████████████████████
```

或者：

```text
        /\
      /    \
_____/      \____
```

前者意味着尺度/车道识别有问题，后者意味着所有车道被压成一个连续模糊分布。

峰间距应该接近：

$$
W_{lane}
$$

---

# 32. Gate 3：纵向一致性

得到：

$$
Z_t
$$

比较：

$$
-\frac{dZ}{dt}
$$

与：

$$
v_{ego}-v_{target}
$$

如果目标假设暂时静止：

$$
v_{target}\approx0
$$

那么：

$$
-\frac{dZ}{dt}\approx v_{ego}
$$

这里不要求非常精确。

你只是检查：

> **数量级和方向对不对。**

例如：

HUD：

```text
30 m/s
```

而视觉：

```text
Z 每秒只下降 2m
```

那一定有尺度问题。

如果：

```text
Z 每秒下降 28~34m
```

则至少说明纵向尺度大体成立。

---

# 33. Gate 4：跨 session 复现

这是我认为最有价值的最终验证。

至少：

```text
Session A
Session B
Session C
```

独立标定。

得到：

```text
yh:
309
312
310

H:
1.72
1.69
1.71

f:
...
```

然后看：

$$
CV=\frac{\sigma}{\mu}
$$

### 成功

参数形成窄带。

### 失败

如果：

```text
yh:
270
312
349
```

但相机确实刚性固定：

> 标定器不稳定。

不要拿 downstream accuracy 掩盖这个问题。

---

# 34. 再加一个非常有价值的“留出验证”

不要把所有车道线都拿来拟合。

例如：

```text
70% line evidence → calibration
30% line evidence → validation
```

然后检查：

$$
\hat W(Z)
$$

是否在留出数据上仍然稳定。

这比单纯看 fitting residual 更重要。

因为你的目标不是：

> “能把自己拟合的数据解释好。”

而是：

> “这个固定相机模型能不能解释没参与拟合的道路。”

---

# 35. RANSAC 应该用在哪里

我建议至少三个地方：

### VP

线段 → VP：

$$
RANSAC
$$

### lane width

$$
\Delta u,v\rightarrow H
$$

用：

$$
RANSAC/Huber
$$

### 最终目标轨迹

不要用于 calibration，而是用于检测异常目标：

```text
trajectory residual
```

---

# 36. 不建议做的东西

## ① 单目深度网络

**排除。**

你的目标不是：

$$
RGB\rightarrow dense\ depth
$$

而是：

$$
RGB + known\ planar\ structure
\rightarrow metric\ coordinates
$$

道路平面 + VP + lane width 已经提供了比深度网络更强的结构先验。

引入深度网络会增加：

* 模型依赖
* 推理成本
* domain gap
* 游戏画面泛化问题
* 不可解释误差

而且你最终还是得解决 metric scale。

**杀鸡用牛刀。**

---

## ② SLAM

**排除。**

你没有：

* 需要构建的地图
* 需要长期定位的需求
* 需要 6DoF camera trajectory 的需求

而且你的场景已经告诉我们：

> 相机基本固定。

SLAM 解决的是：

$$
camera\ pose + map
$$

你只需要：

$$
ground\ plane\ mapping
$$

完全不是一个量级的问题。

---

## ③ 学习式车道回归

第一版**排除**。

不是因为它不能工作。

而是因为你的数据已经有：

> 清晰车道线 + 固定相机 + 强透视结构。

你现在最需要的是验证：

> **几何模型是否成立。**

学习模型反而会把：

```text
几何错误
```

和：

```text
模型错误
```

混在一起。

---

## ④ 目标运动学拟合

**明确排除 calibration。**

可以作为：

> downstream sanity check

但不能作为：

> calibration source

你的第一次实验已经提供了非常有价值的反证。

---

## ⑤ 全局三次曲线替代 homography

也不建议。

三次多项式可以描述：

> 车道在图像中的形状

但不能自然解决：

> 像素 → metric ground coordinate。

它应该是：

```text
IPM
 ↓
lane curve fitting
```

而不是：

```text
lane polynomial
 ↓
代替 camera calibration
```

---

# 37. 我认为真正值得做的 MVP

如果让我给 coding agent 一个非常小的实验任务，我甚至不会让它实现完整系统。

只做：

```text
Experiment/
├── input frames
├── line evidence
├── detect_vp.py
├── fit_ground_model.py
├── validate_lane_width.py
├── validate_tracks.py
└── report.json
```

最终只输出：

```json
{
  "cx": 640,
  "horizon_y": 312.4,
  "camera_height": "...",
  "focal_scale": "...",

  "vp_rmse": "...",
  "lane_width_rmse": "...",

  "stationary_lateral_median": "...",
  "lane_peak_spacing": "...",
  "longitudinal_scale_error": "...",

  "session_parameter_cv": "..."
}
```

**甚至先不要接机器人运行时。**

---

# 38. 最小实验的真正目标

我会把第一轮实验压缩成一个问题：

> **仅凭 10～30 秒平直路面的静态车道线证据，能不能得到一个跨 session 稳定的 \(y_h\) 和 metric ground mapping？**

如果答案是：

### YES

再做：

```text
pitch correction
↓
curved road
↓
HUD speed validation
↓
runtime integration
```

如果答案是：

### NO

也很好。

因为你会马上知道问题到底是：

```text
VP 不稳定
```

还是：

```text
lane width 不够
```

还是：

```text
focal scale 不可观
```

还是：

```text
游戏相机根本不是理想 pinhole
```

而不是继续堆一个更复杂的模型。

---

# 39. 一个我特别建议你加的诊断实验

**人工构造一张“已知答案”的合成图。**

不是为了训练，而是为了验证你的数学实现。

例如设：

```text
cx = 640
yh = 300
H  = 1.5m
f  = 900px
lane width = 3.5m
```

生成：

```text
X = -3.5 ... +3.5
Z = 5 ... 100m
```

投影成像素。

然后让你的标定器从这些像素反推：

$$
(u,v)\rightarrow(X,Z)
$$

如果连这个都不能回到：

```text
X ± ε
Z ± ε
```

就不要碰真实视频。

这是最快的“数学实现正确性”闸门。

---

# 40. 最后一个重要的“待实测”清单

下面这些现在**不要假定为真**：

| 项目                  | 当前状态    |
| ------------------- | ------- |
| 相机高度 H 固定           | **待实测** |
| camera pitch 固定     | **待实测** |
| camera roll ≈ 0     | **待实测** |
| \(c_x=640\) 足够准确    | **待实测** |
| 游戏镜头符合 pinhole      | **待实测** |
| lens distortion 可忽略 | **待实测** |
| 车道宽在整个地图一致          | **待实测** |
| HUD 米制速度确实对应世界位移    | **待实测** |
| 弯道局部仍可用固定 IPM       | **待实测** |
| 跳台/坡道能通过 VP/车道宽残差识别 | **待实测** |

尤其最后三个，不应该从现实道路经验直接套到游戏里。

---

## 我的最终推荐架构

可以浓缩成这一条：

```text
                 ┌── 车道线 / 护栏
                 │
                 ▼
          VP + horizon estimation
                 │
                 ▼
          static camera calibration
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
  metric ground        flat-confidence
   projection              │
       │                   │
       ▼                   ▼
   (X, Z) target      valid / invalid
       │
       ▼
 lane center / lane #
       │
       ▼
 lateral offset
```

而动态信息放在旁边：

```text
HUD speed ───────────────► longitudinal sanity check
frame-to-frame VP ───────► pitch drift correction
target trajectory ───────► calibration validation
```

**不要让它们反过来决定 calibration。**

这个分层正好避开了你第一次实验的根因。

从已有资料看，这条路线也不是“凭经验拼凑”：Hough/VP 车道检测有成熟文献基础，IPM + RANSAC 曲线拟合已有车辆实验，single-view metrology 给出了无需标定板、利用消失线/消失点和已知尺度恢复平面测量的理论基础，而 OpenCV 本身提供 Hough、Homography、RANSAC 这些成熟实现。([数字对象标识符][12])

**因此我不会建议你现在继续研究“更聪明的模型”。下一步最有价值的是做上面这个最小离线实验，尤其先验证 `VP → lane width → ground mapping → 四级 Gate` 这条链能否闭合。**

[1]: https://www.microsoft.com/en-us/research/publication/single-view-metrology-4/?utm_source=chatgpt.com "Single view metrology - Microsoft Research"
[2]: https://www.sciencedirect.com/science/article/abs/pii/S0734189X87802050?utm_source=chatgpt.com "Road following using vanishing points - ScienceDirect"
[3]: https://docs.opencv.org/4.x/d9/db0/tutorial_hough_lines.html?utm_source=chatgpt.com "OpenCV: Hough Line Transform"
[4]: https://pmc.ncbi.nlm.nih.gov/articles/PMC6479783/?utm_source=chatgpt.com "On the Image Sensor Processing for Lane Detection and Control in Vehicle Lane Keeping Systems - PMC"
[5]: https://trid.trb.org/View/1631914?utm_source=chatgpt.com "Lane Detection of Curving Road for Structural Highway With Straight-Curve Model on Vision - TRID"
[6]: https://www.sciencedirect.com/science/article/pii/S092523121630100X?utm_source=chatgpt.com "An accurate and practical calibration method for roadside camera using two vanishing points - ScienceDirect"
[7]: https://docs.opencv.org/doc/doxygen/html/d2/d48/group__d__projection.html?utm_source=chatgpt.com "OpenCV: 3D vision functionality"
[8]: https://www.sciencedirect.com/science/article/abs/pii/S0263224123000453?utm_source=chatgpt.com "One estimation method of road slope and vehicle distance - ScienceDirect"
[9]: https://pmc.ncbi.nlm.nih.gov/articles/PMC6187376/?utm_source=chatgpt.com "Motion Constraints and Vanishing Point Aided Land Vehicle Navigation - PMC"
[10]: https://patents.google.com/patent/EP3901821A1/en?utm_source=chatgpt.com "EP3901821A1 - Method and device for calibrating pitch of camera on vehicle and method and device for continual learning of vanishing point estimation model to be used for calibrating the pitch - Google Patents"
[11]: https://patents.google.com/patent/US8269833B2/en?utm_source=chatgpt.com "US8269833B2 - Method and system for measuring vehicle speed based on movement of video camera - Google Patents"
[12]: https://doi.org/10.1109/TITS.2017.2679222?utm_source=chatgpt.com "A Robust Lane Detection Method Based on Vanishing Point Estimation Using the Relevance of Line Segments"
