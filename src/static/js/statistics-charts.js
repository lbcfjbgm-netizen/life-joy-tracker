document.addEventListener("DOMContentLoaded", function () {
  Chart.register(ChartDataLabels);

  // ---------------------------------------------------------------------
  // 主题配色
  // 图表里的轴线、刻度、图例、数据标签原来写死成浅灰（#D1D5DB），在暗色
  // 底上好看，换到亮色主题（html[data-theme="light"]，暖纸色底）就糊成一团。
  // 这里按当前主题给两套颜色，所有图在建好后统一刷一遍；切换主题时也再刷，
  // 不改 static/css、不用半透明模糊。
  // ---------------------------------------------------------------------
  const THEME_COLORS = {
    dark: {
      ink: "#D1D5DB",
      grid: "rgba(255, 255, 255, 0.10)",
      arcStroke: "rgba(255, 255, 255, 0.12)",
      labelStroke: "rgba(0, 0, 0, 0.55)",
    },
    light: {
      // 暖纸底上要用深墨色，浅灰读不出来
      ink: "#4f483e",
      grid: "rgba(48, 34, 18, 0.13)",
      arcStroke: "rgba(48, 34, 18, 0.14)",
      labelStroke: "rgba(255, 253, 248, 0.9)",
    },
  };

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light"
      : "dark";
  }

  // 单一取色入口：图里所有「文字类」颜色一律走这里，返回当前主题对应的色值。
  // 配置对象里不再写死任何 #D1D5DB —— 写死就等于只给暗色主题调色。
  function axisColor() {
    return THEME_COLORS[currentTheme()].ink;
  }

  // 圆环切片上的标签描边（暗底用黑描边、纸底用纸色描边）
  function labelStrokeColor() {
    return THEME_COLORS[currentTheme()].labelStroke;
  }

  // 已经建好的图，换主题时统一重刷（Chart.js 实例不重复创建）
  const charts = [];

  // 把当前主题的颜色写进一份 options 里。
  // 注意：这套改动必须在 new Chart() **之前**做。
  // 之前是在建完图后立刻 chart.update("none") 刷颜色，结果饼图首帧的
  // datalabels 格式化拿到还没有 dataset 的 ctx，抛 TypeError 打断整个脚本，
  // 后面的图一张都没建出来（页面上只剩第一个饼图）。
  function applyThemeColors(options, chartType) {
    const c = THEME_COLORS[currentTheme()];

    ["x", "y", "y1", "r"].forEach((axis) => {
      const scale = options.scales && options.scales[axis];
      if (!scale) return;
      if (scale.grid && scale.grid.display !== false) scale.grid.color = c.grid;
      if (scale.ticks) scale.ticks.color = c.ink;
      if (scale.title) scale.title.color = c.ink;
    });

    const plugins = options.plugins || {};
    if (plugins.legend && plugins.legend.labels) {
      plugins.legend.labels.color = c.ink;
    }
    if (plugins.datalabels) {
      plugins.datalabels.color = c.ink;
      // 圆环切片上的标签要靠描边压住底色，暗底用黑描边、纸底用纸色描边
      if (plugins.datalabels.formatter) {
        plugins.datalabels.textStrokeColor = c.labelStroke;
      }
    }
    if (plugins.title) plugins.title.color = c.ink;

    // 饼图切片的描边色存在 dataset 上，这里只记下目标色，建图后由
    // paintChart 补（建图时 dataset.borderColor 还没被 Chart 接管）
    options.__themeArcStroke =
      chartType === "pie" || chartType === "doughnut" ? c.arcStroke : null;

    return options;
  }

  // 已经建好的图：换主题时重新配一次色再重绘
  function paintChart(chart) {
    const c = THEME_COLORS[currentTheme()];
    const options = chart.options || {};

    ["x", "y", "y1", "r"].forEach((axis) => {
      const scale = options.scales && options.scales[axis];
      if (!scale) return;
      if (scale.grid && scale.grid.display !== false) scale.grid.color = c.grid;
      if (scale.ticks) scale.ticks.color = c.ink;
      if (scale.title) scale.title.color = c.ink;
    });

    const plugins = options.plugins || {};
    if (plugins.legend && plugins.legend.labels) {
      plugins.legend.labels.color = c.ink;
    }
    if (plugins.datalabels) {
      plugins.datalabels.color = c.ink;
      // 这里绝对不能写成 if (plugins.datalabels.formatter)。
      // chart.options 是 Chart.js 的「脚本化属性」代理：在上面读一个函数属性
      // 不等于把它取回来，而是当场用当前上下文把它**执行一遍**。
      // 饼图的 formatter 被这样调用时 ctx 不完整 → 抛
      //   TypeError: Cannot convert object to primitive value
      // 嵌套再解析一层 → 抛
      //   Error: Recursion detected: formatter->formatter
      // 这两条就是切主题时 console 冒出来的报错（本函数正好在重绘路径上）。
      // 要判断「这张图到底配没配 formatter」，只能读 chart.config.options
      // 这个原始对象（不是代理），读它不会触发执行。
      const rawDatalabels =
        chart.config &&
        chart.config.options &&
        chart.config.options.plugins &&
        chart.config.options.plugins.datalabels;
      if (rawDatalabels && typeof rawDatalabels.formatter === "function") {
        plugins.datalabels.textStrokeColor = c.labelStroke;
      }
    }
    if (plugins.title) plugins.title.color = c.ink;

    if (chart.config.type === "pie" || chart.config.type === "doughnut") {
      chart.data.datasets.forEach((dataset) => {
        dataset.borderColor = c.arcStroke;
      });
    }

    // 换主题是用户主动点的，此时图已经渲染过一帧，ctx.dataset 齐全，
    // update 不会再触发首帧那个坑；万一某个图状态怪，也只是一张图不重绘。
    // 逐张 try：原来是一张抛错就整批断掉（实测饼图 datalabels 的 formatter
    // 在 update 时拿到 undefined 的 ctx.dataset，抛 TypeError，后面的柱图
    // 一张都没刷成暗色）。现在单张失败只影响它自己。
    try {
      chart.update("none");
    } catch (error) {
      /* 单张图刷新失败不影响其它图 */
    }
  }

  // 换主题只走下面这一个入口：盯 html[data-theme] 属性。
  //
  // 以前这里还包了一层 window.__toggleTheme，在调用后立刻
  // charts.forEach(paintChart)。两句都是错的：
  //   1) 时机错 —— 现在 __toggleTheme 是异步的（document.startViewTransition
  //      要等浏览器拍完旧画面快照才执行 apply()），包装层跑的时候主题压根还
  //      没变，画出来仍是旧配色，纯属白跑一遍；
  //   2) 重画两次 —— 属性真变了以后 MutationObserver 又要再画一遍，两次重绘
  //      撞在 Chart.js 的 scriptable 解析上，叠加成报错。
  // 所以包装层整个删掉，只留观察器：属性变了才画，一次主题切换只画一次。
  //
  // 兜底面没有变小：不管谁改的 data-theme（顶栏按钮、hx-boost 局部跳转后重设、
  // 页面里别的脚本直接改属性），观察器都能看到。
  if (typeof MutationObserver === "function") {
    let lastTheme = currentTheme();
    new MutationObserver(function () {
      const now = currentTheme();
      if (now === lastTheme) return;
      lastTheme = now;
      charts.forEach(paintChart);
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  }

  // Custom external tooltip for bar charts
  function customBarTooltip(context) {
    // External custom tooltip
    let tooltipEl = document.getElementById("chartjs-tooltip");

    // Create element if it doesn't exist
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-tooltip";
      tooltipEl.innerHTML = "<table></table>";
      document.body.appendChild(tooltipEl);
    }

    // Hide if no tooltip
    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    // Set Text
    if (tooltipModel.body) {
      const chart = context.chart;
      const dataIndex = tooltipModel.dataPoints[0].dataIndex;
      const title = tooltipModel.title[0] || "";

      // Format title based on chart type
      let formattedTitle = title;
      if (chart.canvas.id === "scoreStackedChart") {
        const score = parseInt(title);
        if (score === 10) {
          formattedTitle = `Score: 10`;
        } else if (score < 0) {
          // 负分桶：模型允许 -10..10，桶按 int() 取整，-10 桶装 -10.0～-10.9。
          // 写成「-10.0～-10.9」而不是「-10.0--10.9」，避免两个减号看混。
          formattedTitle = `Score: ${score}.0～${score}.9`;
        } else {
          formattedTitle = `Score: ${score}.0-${score}.9`;
        }
      }

      // Get all values for this stack
      let tableBody =
        '<thead><tr><th colspan="2">' +
        formattedTitle +
        "</th></tr></thead><tbody>";
      let stackTotal = 0;

      chart.data.datasets.forEach((dataset, i) => {
        if (dataset.data[dataIndex] && dataset.data[dataIndex] > 0) {
          const value = dataset.data[dataIndex];
          stackTotal += value;
          const bgColor = dataset.backgroundColor;
          const label = ZH_STATUS_LABELS[dataset.label] || dataset.label || "";

          tableBody +=
            "<tr>" +
            '<td style="padding-right:15px;"><span style="display:inline-block;width:12px;height:12px;background:' +
            bgColor +
            ';margin-right:8px;border-radius:2px;"></span>' +
            label +
            ":</td>" +
            '<td style="text-align:right;font-weight:bold;">' +
            value +
            "</td>" +
            "</tr>";
        }
      });

      // Add total row
      tableBody +=
        '<tr class="total-row">' +
        "<td>Total:</td>" +
        '<td style="text-align:right;font-weight:bold;">' +
        stackTotal +
        "</td>" +
        "</tr>";

      tableBody += "</tbody>";

      const tableRoot = tooltipEl.querySelector("table");
      tableRoot.innerHTML = tableBody;
    }

    // Position and style the tooltip
    const position = context.chart.canvas.getBoundingClientRect();

    // Set tooltip styles
    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  // Custom external tooltip for pie charts
  function customPieTooltip(context) {
    // External custom tooltip
    let tooltipEl = document.getElementById("chartjs-pie-tooltip");

    // Create element if it doesn't exist
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-pie-tooltip";
      document.body.appendChild(tooltipEl);
    }

    // Hide if no tooltip
    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    // Set Text
    if (tooltipModel.body) {
      const dataPoint = tooltipModel.dataPoints[0];
      const label = dataPoint.label;
      const value = dataPoint.raw;

      // Calculate percentage
      const dataset = context.chart.data.datasets[dataPoint.datasetIndex];
      const total = dataset.data.reduce((sum, val) => sum + val, 0);
      const percentage = Math.round((value / total) * 100);

      // Create tooltip content
      let tooltipContent = `
        <div class="pie-label">${label}</div>
        <div class="pie-value">Count: ${value}</div>
        <div class="pie-percent">${percentage}%</div>
      `;

      tooltipEl.innerHTML = tooltipContent;
    }

    // Position and style the tooltip
    const position = context.chart.canvas.getBoundingClientRect();

    // Set tooltip styles
    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  // Common configuration for pie charts
  const pieChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      datalabels: {
        color: axisColor(),
        font: { size: 12 },
        formatter: (value, ctx) => {
          // ctx.dataset 在首帧和换主题后的某些 re-render 里会是 undefined，
          // 直接 .data 会抛 TypeError。抛出来会把 paintChart 里那张图的
          // update 打断（表现为换主题后柱图的字还是旧颜色），所以先兜一道。
          const dataset = ctx && ctx.dataset;
          if (!dataset || !Array.isArray(dataset.data)) return "";
          const total = dataset.data.reduce((acc, data) => acc + data, 0);
          const percentage = total > 0 ? Math.round((value / total) * 100) : 0;
          const label = ctx.chart.data.labels[ctx.dataIndex] || "";
          return percentage > 5 ? `${label}\n${percentage}%` : "";
        },
        textAlign: "center",
        textStrokeColor: labelStrokeColor(),
        textStrokeWidth: 2,
        textShadowBlur: 5,
        textShadowColor: "rgba(0,0,0,0.5)",
        padding: 6,
      },
      legend: {
        position: "bottom",
        labels: {
          color: axisColor(),
          padding: 20,
          usePointStyle: true,
          pointStyle: "rectRounded",
          generateLabels: function (chart) {
            const original =
              Chart.overrides.pie.plugins.legend.labels.generateLabels;
            const labels = original.call(this, chart);
            labels.forEach((label, i) => {
              label.text = `${label.text} (${chart.data.datasets[0].data[i]})`;
              label.strokeStyle = "transparent";
            });
            return labels;
          },
        },
        margin: { top: 20 },
      },
      tooltip: {
        enabled: false,
        external: customPieTooltip,
      },
    },
    layout: { padding: { bottom: 10 } },
    elements: {
      arc: {
        borderWidth: 1,
        borderColor: "#d3d3d3",
      },
    },
  };

  // Common configuration for bar charts
  const barChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      x: {
        stacked: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: { color: axisColor() },
      },
      y: {
        stacked: true,
        beginAtZero: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: { color: axisColor(), precision: 0 },
      },
    },
    plugins: {
      legend: {
        position: "bottom",
        labels: {
          color: axisColor(),
          padding: 20,
          boxWidth: 12,
          boxHeight: 12,
          usePointStyle: true,
          pointStyle: "rectRounded",
          textAlign: "center",
          font: {
            size: 12,
            lineHeight: 0.1,
          },
        },
      },
      tooltip: {
        enabled: false, // Disable default tooltip
        mode: "index",
        external: customBarTooltip,
      },
      // Disable datalabels for bar charts
      datalabels: {
        display: false,
      },
    },
    interaction: {
      mode: "index",
      intersect: false,
    },
  };

  // 状态名中文映射（后端标准值 -> 页面显示）
  // 和站点其它地方同一套 4 档：完成态一律叫「已通关」，原来那一档并进「已暂停」。
  // 统计图是按状态值聚合的，拿不到 is_all_achievements，所以这里不出现「全成就」，
  // 免得把没拿满成就的也一起算成「全成就」。
  const ZH_STATUS_LABELS = {
    "Completed": "已通关",
    "In progress": "进行中",
    "In Progress": "进行中",
    "Planning": "已暂停",
    "Paused": "已暂停",
    "Dropped": "已放弃",
  };

  // Helper function to process stacked bar data
  // 原计划档和「已暂停」显示成同一个名字，堆叠图里必须合并成一个数据集，
  // 否则图例里会出现两条「已暂停」。按最终显示名合并，标签互不相同的图（评分图）不受影响。
  function processBarData(chartData) {
    const byLabel = new Map();
    chartData.datasets.forEach((dataset) => {
      const label = ZH_STATUS_LABELS[dataset.label] || dataset.label;
      const existing = byLabel.get(label);
      if (existing) {
        existing.data = existing.data.map((value, i) => value + dataset.data[i]);
      } else {
        byLabel.set(label, {
          label: label,
          data: [...dataset.data],
          backgroundColor: dataset.background_color,
          borderColor: "rgba(255, 255, 255, 0.1)",
          borderRadius: 6,
          borderWidth: 1,
        });
      }
    });

    return {
      labels: chartData.labels,
      datasets: [...byLabel.values()].filter((dataset) =>
        dataset.data.some((value) => value > 0)
      ),
    };
  }

  // Helper function to safely initialize charts
  // 建图前先把当前主题的颜色写进 options（不能建成后再 update，原因见
  // applyThemeColors 的注释），然后登记到 charts 里存档，换主题时统一重刷。
  // 同一个 canvas 已经有图时先销毁旧的，避免重复初始化叠出两张图。
  function initializeChartIfExists(elementId, chartType, data, options) {
    const element = document.getElementById(elementId);
    if (!element) return null;

    const existing = Chart.getChart(element);
    if (existing) existing.destroy();

    const chart = new Chart(element.getContext("2d"), {
      type: chartType,
      data: data,
      options: applyThemeColors(options, chartType),
    });
    // 只补饼图切片的描边色（它挂在 dataset 上），不在这里调用 update()：
    // 首帧就 update 会让 datalabels 拿到还没挂上的 ctx.dataset 而抛错。
    if (chartType === "pie" || chartType === "doughnut") {
      const arcStroke = options.__themeArcStroke;
      if (arcStroke) {
        chart.data.datasets.forEach((dataset) => {
          dataset.borderColor = arcStroke;
        });
      }
    }
    charts.push(chart);
    return chart;
  }

  // Create Media Type Distribution Chart
  const mediaTypeDistributionElement = document.getElementById(
    "media_type_distribution"
  );
  if (mediaTypeDistributionElement) {
    const mediaTypeData = JSON.parse(mediaTypeDistributionElement.textContent);
    initializeChartIfExists(
      "mediaTypeChart",
      "pie",
      mediaTypeData,
      pieChartConfig
    );
  }

  // Create Status Distribution Chart
  const statusPieChartElement = document.getElementById(
    "status_pie_chart_data"
  );
  if (statusPieChartElement) {
    const statusPieData = JSON.parse(statusPieChartElement.textContent);
    initializeChartIfExists(
      "statusChart",
      "pie",
      statusPieData,
      pieChartConfig
    );
  }

  // Create Status Stacked Bar Chart
  const statusDistributionElement = document.getElementById(
    "status_distribution"
  );
  if (statusDistributionElement) {
    const statusData = JSON.parse(statusDistributionElement.textContent);
    initializeChartIfExists(
      "statusStackedChart",
      "bar",
      processBarData(statusData),
      barChartConfig
    );
  }

  // Create Score Stacked Bar Chart
  const scoreDistributionElement =
    document.getElementById("score_distribution");
  if (scoreDistributionElement) {
    const scoreData = JSON.parse(scoreDistributionElement.textContent);
    const scoreChartOptions = JSON.parse(JSON.stringify(barChartConfig)); // Deep clone

    // Add score-specific configurations
    scoreChartOptions.scales.x.title = {
      display: true,
      text: "评分",
      color: axisColor(),
      padding: { top: 10, bottom: 0 },
    };

    scoreChartOptions.scales.y.title = {
      display: true,
      text: "条目数",
      color: axisColor(),
      padding: { top: 0, left: 10 },
    };

    scoreChartOptions.plugins.title = {
      display: true,
      text: `平均评分：${scoreData.average_score}（${
        scoreData.total_scored
      } 条）`,
      color: axisColor(),
      padding: { bottom: 10 },
      font: { size: 14 },
    };

    // Ensure tooltip is properly configured for score chart
    scoreChartOptions.plugins.tooltip = {
      enabled: false,
      mode: "index",
      intersect: false,
      external: customBarTooltip,
    };

    initializeChartIfExists(
      "scoreStackedChart",
      "bar",
      processBarData(scoreData),
      scoreChartOptions
    );
  }

  // ---------------------------------------------------------------------
  // 四张新图：年度完成趋势 / 游玩时长 Top 10 / 成就完成度 / 发行年份分布
  // 数据来自后端 get_extra_charts()，json_script id 是 extra_charts。
  // 数据为空就不初始化，避免空图在页面上画个空框。
  // ---------------------------------------------------------------------
  const extraChartsElement = document.getElementById("extra_charts");
  if (extraChartsElement) {
    let extraCharts = null;
    try {
      extraCharts = JSON.parse(extraChartsElement.textContent);
    } catch (error) {
      extraCharts = null;
    }

    // 只有「对象 + 有 labels + 有 datasets」才算有数据
    const hasChartData = (chartData) =>
      chartData &&
      typeof chartData === "object" &&
      Array.isArray(chartData.labels) &&
      chartData.labels.length > 0 &&
      Array.isArray(chartData.datasets) &&
      chartData.datasets.length > 0;

    // 后端给的是 background_color（单个十六进制），转成 Chart.js 的 backgroundColor
    const buildDatasets = (chartData) =>
      chartData.datasets.map((dataset) => ({
        label: dataset.label,
        data: [...dataset.data],
        backgroundColor: dataset.background_color,
        borderColor: "rgba(255, 255, 255, 0.1)",
        borderRadius: 6,
        borderWidth: 1,
      }));

    // 从 barChartConfig 深拷贝出来的配置会丢掉 tooltip 函数，这里统一补回去
    const restoreTooltip = (options) => {
      options.plugins = options.plugins || {};
      options.plugins.tooltip = {
        enabled: false,
        mode: "index",
        intersect: false,
        external: customBarTooltip,
      };
      return options;
    };

    if (extraCharts && typeof extraCharts === "object") {
      // 1) 年度完成趋势：堆叠柱状图，沿用现有 barChartConfig（深色底、灰字）
      if (hasChartData(extraCharts.yearly_completed)) {
        initializeChartIfExists(
          "yearlyCompletedChart",
          "bar",
          {
            labels: extraCharts.yearly_completed.labels,
            datasets: buildDatasets(extraCharts.yearly_completed),
          },
          barChartConfig
        );
      }

      // 2) 游玩时长 Top 10：横向条形图（indexAxis: 'y'），值直接标在条末
      if (hasChartData(extraCharts.playtime_top)) {
        const playtimeOptions = JSON.parse(JSON.stringify(barChartConfig));
        // JSON 深拷贝会丢掉函数：把自定义 tooltip 装回去，否则整张图没有提示框
        restoreTooltip(playtimeOptions);
        playtimeOptions.indexAxis = "y";
        // 关键：横过来以后必须显式指定轴类型。
        // 只写 indexAxis:"y" 时，Chart.js 仍按「x=类别、y=数值」的默认去解析，
        // 结果数值全挤在 x=0 的位置、条子画不出来（实测过）。所以把 x 定成
        // 数值轴、y 定成类别轴。
        playtimeOptions.scales.x.type = "linear";
        playtimeOptions.scales.y.type = "category";
        // 单数据集不需要堆叠，横过来以后形态会怪
        playtimeOptions.scales.x.stacked = false;
        playtimeOptions.scales.y.stacked = false;
        playtimeOptions.scales.x.beginAtZero = true;
        // 横条图里数值在 x 轴，小数要保留，别被 precision: 0 抹平
        playtimeOptions.scales.x.ticks.precision = undefined;
        playtimeOptions.scales.y.ticks.precision = undefined;
        playtimeOptions.scales.y.grid = { display: false };
        playtimeOptions.plugins.legend = { display: false };
        playtimeOptions.plugins.datalabels = {
          display: true,
          // 颜色交给 applyThemeColors 按主题填，这里只定位置和格式
          color: axisColor(),
          anchor: "end",
          align: "right",
          padding: 4,
          font: { size: 11 },
          // 最长的柱子数值会顶到绘图区右边界，clamp 让它贴着边缘不出界
          clamp: true,
          formatter: (value) => `${value} 小时`,
        };
        // 预留右侧空间：不加的话最长那根柱子的「135 小时」会被裁成「135」
        playtimeOptions.layout = { padding: { right: 64 } };

        initializeChartIfExists(
          "playtimeChart",
          "bar",
          {
            labels: extraCharts.playtime_top.labels,
            datasets: buildDatasets(extraCharts.playtime_top),
          },
          playtimeOptions
        );
      }

      // 3) 成就完成度分布：竖柱状图，三个桶各一种颜色更易读
      if (hasChartData(extraCharts.achievement_buckets)) {
        const achievementOptions = JSON.parse(JSON.stringify(barChartConfig));
        // JSON 深拷贝会丢掉函数：把自定义 tooltip 装回去，否则整张图没有提示框
        restoreTooltip(achievementOptions);
        achievementOptions.scales.x.stacked = false;
        achievementOptions.scales.y.stacked = false;
        achievementOptions.plugins.legend = { display: false };
        achievementOptions.plugins.datalabels = {
          display: true,
          color: axisColor(),
          anchor: "end",
          align: "top",
          font: { size: 12 },
          formatter: (value) => value,
        };

        const achievementData = {
          labels: extraCharts.achievement_buckets.labels,
          datasets: buildDatasets(extraCharts.achievement_buckets),
        };
        // 全成就 / 部分完成 / 无成就数据，用和状态图一致的语义色
        achievementData.datasets[0].backgroundColor = [
          "#eab308",
          "#6366f1",
          "#6b7280",
        ];

        initializeChartIfExists(
          "achievementChart",
          "bar",
          achievementData,
          achievementOptions
        );
      }

      // 4) 发行年份分布：竖柱状图，单数据集、不堆叠
      if (hasChartData(extraCharts.release_years)) {
        const releaseYearOptions = JSON.parse(JSON.stringify(barChartConfig));
        // JSON 深拷贝会丢掉函数：把自定义 tooltip 装回去，否则整张图没有提示框
        restoreTooltip(releaseYearOptions);
        releaseYearOptions.scales.x.stacked = false;
        releaseYearOptions.scales.y.stacked = false;
        releaseYearOptions.plugins.legend = { display: false };
        releaseYearOptions.plugins.datalabels = { display: false };

        initializeChartIfExists(
          "releaseYearChart",
          "bar",
          {
            labels: extraCharts.release_years.labels,
            datasets: buildDatasets(extraCharts.release_years),
          },
          releaseYearOptions
        );
      }
    }
  }

  // ---------------------------------------------------------------------
  // 月度游玩趋势（第 9 张图）
  // 数据来自 app/playtime.get_monthly_playtime()，json_script id 是 monthly_playtime。
  //
  // 这套数据只能向前积累：站点第一次跑采集脚本之前的月份库里没有任何快照，
  // 后端一律填 0 并用 has_data=False 标出来。那种月份**一根柱子都不画**——
  // 画一排 0 会让人误读成「那些月一天没玩」，模板那边已经换成说明卡片，
  // 这里再判一次 has_data，双保险。
  // ---------------------------------------------------------------------
  const monthlyElement = document.getElementById("monthly_playtime");
  if (monthlyElement) {
    let monthly = null;
    try {
      monthly = JSON.parse(monthlyElement.textContent);
    } catch (error) {
      monthly = null;
    }

    const monthlyHasData =
      monthly &&
      typeof monthly === "object" &&
      monthly.has_data === true &&
      Array.isArray(monthly.labels) &&
      monthly.labels.length > 0 &&
      Array.isArray(monthly.datasets) &&
      monthly.datasets.length > 0;

    if (monthlyHasData) {
      const canvas = document.getElementById("monthlyPlaytimeChart");
      if (canvas) {
        // 后端给的是整 12 个月窗口，data_start 之前的月份一律填 0（那时候
        // 库里还没有快照）。从 data_start 那个月开始截断，否则图左边会拖着
        // 一排 0 的假柱子——看起来像「那几个月一天没玩」，和说明卡同一个坑。
        let startIndex = 0;
        if (
          typeof monthly.data_start === "string" &&
          monthly.data_start.length > 0
        ) {
          const idx = monthly.labels.indexOf(monthly.data_start);
          if (idx > 0) startIndex = idx;
        }
        const labels = monthly.labels.slice(startIndex);
        const hoursData = monthly.datasets[0].data.slice(startIndex);
        const gamesData = monthly.datasets[1].data.slice(startIndex);
        const monthlyOptions = JSON.parse(JSON.stringify(barChartConfig));
        // JSON 深拷贝会丢掉函数：把自定义 tooltip 装回去，否则整张图没有提示框
        // （restoreTooltip 定义在上面那个 if 块里，这里作用域拿不到，直接装一份）
        monthlyOptions.plugins = monthlyOptions.plugins || {};
        monthlyOptions.plugins.tooltip = {
          enabled: false,
          mode: "index",
          intersect: false,
          external: customBarTooltip,
        };
        monthlyOptions.scales.x.stacked = false;
        monthlyOptions.scales.y.stacked = false;
        monthlyOptions.scales.x.ticks.precision = undefined;
        monthlyOptions.scales.y.ticks.precision = undefined;
        // 左轴是游玩小时（柱子），右轴是游戏数（折线），两条轴各管一个量纲
        monthlyOptions.scales.y.title = {
          display: true,
          text: "小时",
          color: axisColor(),
          padding: { top: 0, left: 10 },
        };
        monthlyOptions.plugins.datalabels = { display: false };
        // 左右两条轴各带标题，右边留一点空间免得末位标签贴边
        monthlyOptions.layout = { padding: { right: 8 } };

        // 两个数据集量纲差得远（小时 vs 游戏数），分开画在左右两条轴上，
        // 否则游戏数（个位数）会被压成贴地的一条线。
        const hoursDataset = {
          label: monthly.datasets[0].label || "游玩小时",
          data: hoursData,
          backgroundColor: monthly.datasets[0].background_color || "#6366f1",
          borderColor: "rgba(255, 255, 255, 0.10)",
          borderRadius: 6,
          borderWidth: 1,
          yAxisID: "y",
          type: "bar",
        };
        const gamesDataset = {
          label: monthly.datasets[1].label || "玩过的游戏数",
          data: gamesData,
          backgroundColor: monthly.datasets[1].background_color || "#22c55e",
          borderColor: monthly.datasets[1].background_color || "#22c55e",
          // 折线 + 点，和柱子区分开，一眼看出「多少款游戏」而不是「多少小时」
          type: "line",
          borderWidth: 2,
          tension: 0.35,
          pointRadius: 3,
          pointHoverRadius: 5,
          fill: false,
          yAxisID: "y1",
        };

        monthlyOptions.scales.y1 = {
          position: "right",
          beginAtZero: true,
          grid: { display: false },
          ticks: { color: axisColor(), precision: 0 },
          title: {
            display: true,
            text: "游戏数",
            color: axisColor(),
            padding: { top: 0, right: 10 },
          },
        };

        initializeChartIfExists(
          "monthlyPlaytimeChart",
          "bar",
          { labels: labels, datasets: [hoursDataset, gamesDataset] },
          monthlyOptions
        );
      }
    }
  }
});
