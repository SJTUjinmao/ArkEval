export class DateHelper {
  public static sameDay(d1: Date, d2: Date) {
    return d1.getFullYear() === d2.getFullYear() &&
      d1.getMonth() === d2.getMonth() &&
      d1.getDate() === d2.getDate();
  }

  public static sameWeek(d1: Date, d2: Date) {
    let d1Monday = new Date(d1);
    d1Monday.setDate(d1.getDate() - d1.getDay() + (d1.getDay() === 0 ? -6 : 1));
    let d2Monday = new Date(d2);
    d2Monday.setDate(d2.getDate() - d2.getDay() + (d2.getDay() === 0 ? -6 : 1));
    return d1Monday.getTime() === d2Monday.getTime();
  }

  public static sameYear(d1: Date, d2: Date) {
    return d1.getFullYear() === d2.getFullYear();
  }

  public static getParsedDateString(timestamp: number | undefined): string {
    if (!timestamp) {
      return '';
    }
    let today = new Date();
    let date = new Date(timestamp * 1000);
    if (DateHelper.sameDay(today, date)) {
      let hours = date.getHours();
      let minutes = date.getMinutes();
      return `${hours}:${minutes < 10 ? '0' : ''}${minutes}`;
    } else if (DateHelper.sameYear(today, date)) {
      return `${date.getDate()}/${date.getMonth() + 1}`;
    } else {
      return `${date.getDate()}/${date.getMonth() + 1}/${date.getFullYear()}`;
    }

  }
}