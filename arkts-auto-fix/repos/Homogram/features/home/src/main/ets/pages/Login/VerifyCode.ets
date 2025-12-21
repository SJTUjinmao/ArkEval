import { Logger } from '@ohos/base'
import { LoginState, verifyCode, signOut } from 'libhomogrape.so'
import { emitter } from '@kit.BasicServicesKit';
import { EventConstants } from '../../constants/EventConstants';

@Entry
@Component
export struct VerifyCode {
  @State code: string = '';
  @State isError: boolean = false;
  @State isSignOut: boolean = false;
  @State errorMessage: string = '';
  @Consume('pageInfo') pageInfo: NavPathStack;

  build() {
    NavDestination() {
      Column() {
        Text('Please input your verify code').margin({ top: 20 })
        TextInput({ placeholder: 'input your code', text: this.code }).margin({ top: 20 })
          .onChange((value) => {
            this.code = value;
          })
        Button('Sign in').width(150).margin({ top: 20 })
          .onClick(async () => {
            if (this.code.length == 0) {
              this.errorMessage = 'Please input your verify code';
              this.isError = true;
              return;
            }
            Logger.debug('Login/VerifyCode submit code: ' + this.code);
            let result = await verifyCode(this.code);
            if (result == LoginState.LoggedIn) {
              // Index -> PhoneNumber -> VerifyCode
              if (this.pageInfo.size() != 2) {
                this.errorMessage =
                  'Login/VerifyCode LoggedIn but pageInfo size is not 2 (is ' + this.pageInfo.size().toString() + ')';
                Logger.error(this.errorMessage);
                this.isError = true;
                this.isSignOut = await signOut();
                this.errorMessage = 'Sign out: ' + this.isSignOut.toString();
                return
              }
              this.pageInfo.clear();
              emitter.emit(EventConstants.onLoginEvent);
            } else if (result == LoginState.PasswordRequired) {
              Logger.debug('Login/VerifyCode PasswordRequired');
              this.pageInfo.pushPath({ name: 'Login/Password' });
            } else {
              this.errorMessage = 'Login/VerifyCode failed: ' + result.toString();
              Logger.error(this.errorMessage);
              this.isError = true;
            }

            return;
          })
          .bindPopup(this.isError, {
            message: this.errorMessage,
          })
      }.padding(20)
    }
  }
}