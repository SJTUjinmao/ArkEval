import { vibrator } from '@kit.SensorServiceKit';
import { BusinessError } from '@kit.BasicServicesKit';
export class Vibrator {
  public static async kata() {
    let effect = vibrator.EffectId.EFFECT_CLOCK_TIMER;
    try {

      if (vibrator.isSupportEffect) {
        vibrator.isSupportEffect(effect, (err: BusinessError) => {
          if (err) {
            console.error(`Failed to query effect. Code: ${err.code}, message: ${err.message}`);
            return;
          }
          console.info('Succeed in querying effect');
          if (this.ctx) {
            this.ctx.startVibration({
              type: 'preset',
              effectId: effect,
              count: 1,
              intensity: 100,
            }, {
              usage: 'unknown'
            }, (error: BusinessError) => {
              if (error) {
                console.error(`Failed to start vibration. Code: ${error.code}, message: ${error.message}`);
              } else {
                console.info('Succeed in starting vibration');
              }
            });
          }
        });
      } else {
        console.log('Haptic feedback not supported');
      }
    } catch (error) {
      let e: BusinessError = error as BusinessError;
      console.error(`An unexpected error occurred. Code: ${e.code}, message: ${e.message}`);
    }
  }
}
