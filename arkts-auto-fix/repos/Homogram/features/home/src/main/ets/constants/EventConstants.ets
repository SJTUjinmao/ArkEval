import { emitter } from "@kit.BasicServicesKit";

export class EventConstants {
  public static onForegroundEvent: emitter.InnerEvent = {
    eventId: 0
  };
  public static onLoginEvent: emitter.InnerEvent = {
    eventId: 1
  };
}