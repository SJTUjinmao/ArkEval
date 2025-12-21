import { Logger } from '@ohos/base'
import { LoginState, login, signOut } from 'libhomogrape.so'
import { emitter } from '@kit.BasicServicesKit';
import { EventConstants } from '../../constants/EventConstants';

@Entry
@Component
export struct PhoneNumber {
  @State phone: string = '';
  @State isError: boolean = false;
  @State isSignOut: boolean = false;
  @State errorMessage: string = '';
  @Consume('pageInfo') pageInfo: NavPathStack;

  build() {
    NavDestination() {
      Column() {
        Text('Please input your phone number').margin({ top: 20 })
        TextInput({ placeholder: 'input your phone number', text: this.phone }).margin({ top: 20 })
          .onChange((value) => {
            this.phone = value;
          })
        Button('Sign in').width(150).margin({ top: 20 })
          .onClick(async () => {
            if (this.phone.length == 0) {
              this.errorMessage = 'Phone number is empty';
              Logger.error(this.errorMessage);
              this.isError = true;
              return;
            }
            Logger.debug('Login/Phone number: ' + this.phone);
            let result = await login(this.phone);
            if (result == LoginState.LoggedIn) {
              // Index -> PhoneNumber
              if (this.pageInfo.size() != 1) {
                this.errorMessage =
                  'Login/PhoneNumber LoggedIn but pageInfo size is not 1 (is ' + this.pageInfo.size().toString() + ')';
                Logger.error(this.errorMessage);
                this.isError = true;
                this.isSignOut = await signOut();
                this.errorMessage = 'Sign out: ' + this.isSignOut.toString();
                return;
              }
              this.pageInfo.clear();
              emitter.emit(EventConstants.onLoginEvent);
            } else if (result == LoginState.CodeRequired) {
              this.pageInfo.pushPath({ name: 'Login/VerifyCode' });
            } else {
              this.errorMessage = 'VerifyCode failed: ' + result.toString();
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