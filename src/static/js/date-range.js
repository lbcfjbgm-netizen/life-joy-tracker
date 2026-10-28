function dateRangePicker(defaultMode) {
  // defaultMode === "all" 时默认显示「全部时间」，跟服务端不带参数时的默认范围一致
  // （统计页就是这么用的）；日志页不传，仍按最近 12 个月。
  const defaultAll = defaultMode === "all";
  return {
    isOpen: false,
    activeTab: "predefined",
    selectedRange: defaultAll ? "全部时间" : "最近 12 个月",
    startDate: defaultAll
      ? "all"
      : new Date(new Date().setFullYear(new Date().getFullYear() - 1))
          .toISOString()
          .split("T")[0],
    endDate: defaultAll ? "all" : new Date().toISOString().split("T")[0],
    customRangeLabel: "",

    predefinedRanges: [
      { name: "今天" },
      { name: "昨天" },
      { name: "本周" },
      { name: "最近 7 天" },
      { name: "本月" },
      { name: "最近 30 天" },
      { name: "最近 90 天" },
      { name: "今年" },
      { name: "最近 6 个月" },
      { name: "最近 12 个月" },
      { name: "全部时间" },
    ],

    init() {
      // ① 优先读容器上的 data-range-start / data-range-end（统计页用它显示
      //    「上次选的范围」）。没有这两个属性（日志页）就完全不生效。
      const dataStart = this.$el?.dataset?.rangeStart;
      const dataEnd = this.$el?.dataset?.rangeEnd;

      if (dataStart && dataEnd) {
        this.startDate = dataStart;
        this.endDate = dataEnd;
        this.detectRangeFromDates();
        return;
      }

      // ② 老逻辑：从 URL 参数初始化（日志页走这条）
      const urlParams = new URLSearchParams(window.location.search);
      const startDateParam = urlParams.get("start-date");
      const endDateParam = urlParams.get("end-date");

      if (startDateParam && endDateParam) {
        this.startDate = startDateParam;
        this.endDate = endDateParam;

        // Try to determine which predefined range matches these dates
        this.detectRangeFromDates();
      }
    },

    toggleDropdown() {
      this.isOpen = !this.isOpen;
    },

    selectPredefinedRange(rangeName) {
      this.selectedRange = rangeName;
      this.updateDatesFromRange(rangeName);
      this.isOpen = false;
      this.applyDateFilter();
    },

    updateDatesFromRange(rangeName) {
      const today = new Date();
      // Set time to start of day to avoid timezone issues
      today.setHours(0, 0, 0, 0);
      let start = new Date(today);
      let end = new Date(today);

      let shouldFormatDates = true;

      switch (rangeName) {
        case "今天":
          // Both start and end are today
          start = new Date(today);
          end = new Date(today);
          break;

        case "昨天":
          // Both start and end are yesterday
          start = new Date(today);
          start.setDate(start.getDate() - 1);
          end = new Date(start);
          break;

        case "本周":
          // Start from Monday of current week
          const dayOfWeek = today.getDay(); // 0 is Sunday, 1 is Monday, etc.
          const diff = dayOfWeek === 0 ? 6 : dayOfWeek - 1; // Adjust to make Monday the first day
          start = new Date(today);
          start.setDate(start.getDate() - diff);
          end = new Date(today);
          break;

        case "最近 7 天":
          start = new Date(today);
          start.setDate(start.getDate() - 6); // 6 days ago + today = 7 days
          end = new Date(today);
          break;

        case "本月":
          // First day of current month to today
          start = new Date(today.getFullYear(), today.getMonth(), 1);
          end = new Date(today);
          break;

        case "最近 30 天":
          start = new Date(today);
          start.setDate(start.getDate() - 29); // 29 days ago + today = 30 days
          end = new Date(today);
          break;

        case "最近 90 天":
          start = new Date(today);
          start.setDate(start.getDate() - 89); // 89 days ago + today = 90 days
          end = new Date(today);
          break;

        case "今年":
          // January 1st of current year to today
          start = new Date(today.getFullYear(), 0, 1);
          end = new Date(today);
          break;

        case "最近 6 个月":
          start = new Date(today);
          start.setMonth(start.getMonth() - 6);
          // If the day doesn't exist in the target month, it will roll over
          if (start.getDate() !== today.getDate()) {
            // If the days don't match, we rolled over to the next month
            // Set to the last day of the previous month
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          end = new Date(today);
          break;

        case "最近 12 个月":
          start = new Date(today);
          start.setFullYear(start.getFullYear() - 1);
          // Handle the same day-of-month issue as with 6 months
          if (start.getDate() !== today.getDate()) {
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          end = new Date(today);
          break;

        case "全部时间":
          this.startDate = "all";
          this.endDate = "all";
          shouldFormatDates = false;
          break;
      }

      // Format dates as YYYY-MM-DD strings only if not "全部时间"
      if (shouldFormatDates) {
        this.startDate = this.formatDateForInput(start);
        this.endDate = this.formatDateForInput(end);
      }
    },

    formatDateForInput(date) {
      // Format date as YYYY-MM-DD
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    },

    updateDateRange() {
      // Ensure end date is not before start date
      if (
        this.parseInputDate(this.endDate) < this.parseInputDate(this.startDate)
      ) {
        this.endDate = this.startDate;
      }

      this.customRangeLabel = `${this.formatDisplayDate(
        this.startDate
      )} - ${this.formatDisplayDate(this.endDate)}`;
    },

    applyCustomRange() {
      this.selectedRange = this.customRangeLabel;
      this.isOpen = false;
      this.applyDateFilter();
    },

    clampDateInput(value) {
      // 年份越界就夹到边界：统计页后端把 1900 之前当作无限制，而 0001-01-01
      // 会踩到 Asia/Shanghai 公元 1 年的 LMT 时区（+08:05:43），转 UTC 时下溢
      // 直接 500；2999 之后同理没有意义。只在真的越界时才动值：
      // 「全部时间」的 "all" 和正常日期一律原样返回，日志页的行为不受影响。
      if (!value || value === "all") {
        return value;
      }

      const year = Number(String(value).split("-")[0]);
      if (!Number.isFinite(year)) {
        return value;
      }

      if (year < 1900) {
        return "1900-01-01";
      }

      if (year > 2999) {
        return "2999-12-31";
      }

      return value;
    },

    applyDateFilter() {
      // 导航前先拦一道越界年份（浏览器 input 的 min/max 只在手动输入时生效，
      // 按钮直接改 startDate/endDate 的路径挡不住）。
      this.startDate = this.clampDateInput(this.startDate);
      this.endDate = this.clampDateInput(this.endDate);

      // 夹完之后两端可能反过来（比如结束日期本来在 1900 之前），跟
      // updateDateRange() 一样把结束拉到开始，别把倒序范围发出去。
      if (
        this.startDate !== "all" &&
        this.endDate !== "all" &&
        this.parseInputDate(this.endDate) < this.parseInputDate(this.startDate)
      ) {
        this.endDate = this.startDate;
      }

      // Create URL with date parameters
      const url = new URL(window.location.href);
      url.searchParams.set("start-date", this.startDate);
      url.searchParams.set("end-date", this.endDate);

      // Navigate to the URL
      window.location.href = url.toString();
    },

    formatDisplayDate(dateString) {
      const date = this.parseInputDate(dateString);
      const format = this.getDateFormat();

      return this.formatDateByDjangoFormat(date, format);
    },

    getDateFormat() {
      const scriptTag = document.querySelector('script[data-date-format]');
      const selectedFormat = scriptTag?.dataset.dateFormat;
      const dateFormats = this.getDateFormatValues();

      if (
        selectedFormat &&
        (!dateFormats.length || dateFormats.includes(selectedFormat))
      ) {
        return selectedFormat;
      }

      return dateFormats[0] || "Y-m-d";
    },

    getDateFormatValues() {
      const formatsElement = document.getElementById("date_format_values");

      if (!formatsElement?.textContent) {
        return [];
      }

      try {
        const dateFormats = JSON.parse(formatsElement.textContent);
        return Array.isArray(dateFormats) ? dateFormats : [];
      } catch {
        return [];
      }
    },

    parseInputDate(dateString) {
      const [year, month, day] = dateString.split("-").map(Number);
      return new Date(year, month - 1, day);
    },

    formatDateByDjangoFormat(date, djangoFormat) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      const shortMonth = date.toLocaleString(undefined, { month: "short" });
      const longMonth = date.toLocaleString(undefined, { month: "long" });
      const shortWeekday = date.toLocaleString(undefined, { weekday: "short" });
      const longWeekday = date.toLocaleString(undefined, { weekday: "long" });
      const ordinalSuffix = this.getOrdinalSuffix(date.getDate());

      const formatters = {
        d: () => day,
        D: () => shortWeekday,
        F: () => longMonth,
        j: () => String(date.getDate()),
        l: () => longWeekday,
        m: () => month,
        M: () => shortMonth,
        n: () => String(date.getMonth() + 1),
        S: () => ordinalSuffix,
        y: () => String(year).slice(-2),
        Y: () => String(year),
      };

      let formattedDate = "";
      let isEscaped = false;

      for (const character of djangoFormat) {
        if (isEscaped) {
          formattedDate += character;
          isEscaped = false;
        } else if (character === "\\") {
          isEscaped = true;
        } else {
          formattedDate += formatters[character]?.() ?? character;
        }
      }

      return formattedDate;
    },

    getOrdinalSuffix(day) {
      if (day >= 11 && day <= 13) {
        return "th";
      }

      switch (day % 10) {
        case 1:
          return "st";
        case 2:
          return "nd";
        case 3:
          return "rd";
        default:
          return "th";
      }
    },

    detectRangeFromDates() {
      // Check for All Time (arbitrary start date)
      if (this.startDate === "all" && this.endDate === "all") {
        this.selectedRange = "全部时间";
        return;
      }
      // Parse the current start and end dates
      const startDate = this.parseInputDate(this.startDate);
      const endDate = this.parseInputDate(this.endDate);

      // Get today's date with time set to 00:00:00
      const today = new Date();
      today.setHours(0, 0, 0, 0);

      // Helper function to check if two dates are the same day
      const isSameDay = (d1, d2) => {
        return (
          d1.getFullYear() === d2.getFullYear() &&
          d1.getMonth() === d2.getMonth() &&
          d1.getDate() === d2.getDate()
        );
      };

      // Check for Today
      if (isSameDay(startDate, today) && isSameDay(endDate, today)) {
        this.selectedRange = "今天";
        return;
      }

      // Check for Yesterday
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      if (isSameDay(startDate, yesterday) && isSameDay(endDate, yesterday)) {
        this.selectedRange = "昨天";
        return;
      }

      // Check for This Week
      const thisWeekStart = new Date(today);
      const dayOfWeek = today.getDay();
      const diffToMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
      thisWeekStart.setDate(today.getDate() - diffToMonday);
      if (isSameDay(startDate, thisWeekStart) && isSameDay(endDate, today)) {
        this.selectedRange = "本周";
        return;
      }

      // Check for Last 7 Days
      const last7DaysStart = new Date(today);
      last7DaysStart.setDate(today.getDate() - 6);
      if (isSameDay(startDate, last7DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "最近 7 天";
        return;
      }

      // Check for This Month
      const thisMonthStart = new Date(today.getFullYear(), today.getMonth(), 1);
      if (isSameDay(startDate, thisMonthStart) && isSameDay(endDate, today)) {
        this.selectedRange = "本月";
        return;
      }

      // Check for Last 30 Days
      const last30DaysStart = new Date(today);
      last30DaysStart.setDate(today.getDate() - 29);
      if (isSameDay(startDate, last30DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "最近 30 天";
        return;
      }

      // Check for Last 90 Days
      const last90DaysStart = new Date(today);
      last90DaysStart.setDate(today.getDate() - 89);
      if (isSameDay(startDate, last90DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "最近 90 天";
        return;
      }

      // Check for This Year
      const thisYearStart = new Date(today.getFullYear(), 0, 1);
      if (isSameDay(startDate, thisYearStart) && isSameDay(endDate, today)) {
        this.selectedRange = "今年";
        return;
      }

      // Check for Last 6 Months
      // This is more complex due to varying month lengths
      const last6MonthsStart = new Date(today);
      last6MonthsStart.setMonth(today.getMonth() - 6);
      // Adjust for month length differences
      if (last6MonthsStart.getDate() !== today.getDate()) {
        // We rolled over to the next month, adjust to last day of previous month
        last6MonthsStart.setDate(0);
      }

      // Allow a 1-day difference for month-end variations
      const isWithinOneDay = (d1, d2) => {
        const diff = Math.abs(d1.getTime() - d2.getTime());
        return diff <= 86400000; // 24 hours in milliseconds
      };

      if (
        isWithinOneDay(startDate, last6MonthsStart) &&
        isSameDay(endDate, today)
      ) {
        this.selectedRange = "最近 6 个月";
        return;
      }

      // Check for Last 12 Months
      const last12MonthsStart = new Date(today);
      last12MonthsStart.setFullYear(today.getFullYear() - 1);
      // Adjust for month length differences and leap years
      if (last12MonthsStart.getDate() !== today.getDate()) {
        last12MonthsStart.setDate(0);
      }

      if (
        isWithinOneDay(startDate, last12MonthsStart) &&
        isSameDay(endDate, today)
      ) {
        this.selectedRange = "最近 12 个月";
        return;
      }

      // If no match found, use custom range format
      this.customRangeLabel = `${this.formatDisplayDate(
        this.startDate
      )} - ${this.formatDisplayDate(this.endDate)}`;
      this.selectedRange = this.customRangeLabel;
    },
  };
}
