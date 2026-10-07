/* TREC 20-19 deadline math. Every rule here is sourced; don't change one
 * without re-reading its source.
 *  - Counting: Effective Date is day 0, calendar days (Kerri Lewis, former
 *    TREC general counsel, Texas A&M Real Estate Center, "Option Period Basics").
 *  - Earnest money + option fee: within 3 days after the Effective Date;
 *    additional earnest money: within N days. If the last day is a Saturday,
 *    Sunday, or Legal Holiday, extended to the end of the next day that isn't
 *    (TREC 20-19 Paragraph 5A and 5A(2)).
 *  - Option Period: N days after the Effective Date, notice by 5:00 p.m. local
 *    time; no weekend/holiday extension (TREC 20-19 Paragraph 5B).
 *  - "Legal Holiday" = Tex. Gov't Code 662.003(a) national holidays plus
 *    (b)(4) June 19 and (b)(6) the Friday after Thanksgiving, on their
 *    statutory dates (no "observed" Monday rule in that definition).
 */
(function (root) {
  function d(y, m, day) { return new Date(Date.UTC(y, m, day)); }
  function nthWeekday(y, m, weekday, n) {
    var first = d(y, m, 1), offset = (weekday - first.getUTCDay() + 7) % 7;
    return d(y, m, 1 + offset + 7 * (n - 1));
  }
  function lastWeekday(y, m, weekday) {
    var last = d(y, m + 1, 0), offset = (last.getUTCDay() - weekday + 7) % 7;
    return d(y, m, last.getUTCDate() - offset);
  }
  function key(date) { return date.toISOString().slice(0, 10); }

  function legalHolidays(y) {
    var thanksgiving = nthWeekday(y, 10, 4, 4);
    var list = [
      [d(y, 0, 1), "New Year's Day"],
      [nthWeekday(y, 0, 1, 3), "Martin Luther King, Jr., Day"],
      [nthWeekday(y, 1, 1, 3), "Presidents' Day"],
      [lastWeekday(y, 4, 1), "Memorial Day"],
      [d(y, 5, 19), "Emancipation Day in Texas"],
      [d(y, 6, 4), "Independence Day"],
      [nthWeekday(y, 8, 1, 1), "Labor Day"],
      [d(y, 10, 11), "Veterans Day"],
      [thanksgiving, "Thanksgiving Day"],
      [d(y, 10, thanksgiving.getUTCDate() + 1), "the Friday after Thanksgiving"],
      [d(y, 11, 25), "Christmas Day"]
    ];
    var map = {};
    list.forEach(function (h) { map[key(h[0])] = h[1]; });
    return map;
  }

  function addDays(date, n) { return d(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + n); }

  function whyNotBusinessDay(date) {
    var dow = date.getUTCDay();
    if (dow === 6) return "Saturday";
    if (dow === 0) return "Sunday";
    return legalHolidays(date.getUTCFullYear())[key(date)] || null;
  }

  // Paragraph 5A(2): roll forward past Saturdays, Sundays, Legal Holidays.
  function earnestDeadline(effective, days) {
    var original = addDays(effective, days), date = original, skipped = [];
    var reason = whyNotBusinessDay(date);
    while (reason) { skipped.push({ date: key(date), reason: reason }); date = addDays(date, 1); reason = whyNotBusinessDay(date); }
    return { date: key(date), original: key(original), extended: skipped.length > 0, skipped: skipped };
  }

  // Paragraph 5B: no extension. Flag (don't move) a weekend/holiday end date.
  function optionDeadline(effective, days) {
    var date = addDays(effective, days);
    return { date: key(date), fallsOn: whyNotBusinessDay(date) };
  }

  function parse(iso) { var p = iso.split("-").map(Number); return d(p[0], p[1] - 1, p[2]); }

  var api = { legalHolidays: legalHolidays, earnestDeadline: earnestDeadline, optionDeadline: optionDeadline, parse: parse };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.TrecDeadlines = api;
})(this);
