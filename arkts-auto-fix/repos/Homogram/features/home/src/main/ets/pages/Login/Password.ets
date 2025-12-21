import { Logger } from '@ohos/base'
import { LoginState, password, signOut } from 'libhomogrape.so'
import { emitter } from '@kit.BasicServicesKit';
import { EventConstants } from '../../constants/EventConstants';

@Entry
@Component
export struct Password {
  @State password: string = '';
  @State isError: boolean = false;
  @State isSignOut: boolean = false;
  @State errorMessage: string = '';
  @Consume('pageInfo') pageInfo: NavPathStack;

  build() {
    NavDestination() {
      Column() {
        Text('Please input your password').margin({ top: 20 })
        TextInput({ placeholder: 'input your password', text: this.password }).margin({ top: 20 })
          .onChange((value) => {
            this.password = value;
          })
        Button('Sign in').width(150).margin({ top: 20 })
          .onClick(async () => {
            if (this.password.length == 0) {
              this.errorMessage = 'Password is empty';
              Logger.error(this.errorMessage);
              this.isError = true;
              return;
            }
            Logger.debug('Login/Password: ' + this.password);
            let result = await password(this.password);
            if (result == LoginState.LoggedIn) {
              // Index -> PhoneNumber -> VerifyCode -> Password
              if (this.pageInfo.size() != 3) {
                this.errorMessage =
                  'Login/Password LoggedIn but pageInfo size is not 3 (is ' + this.pageInfo.size().toString() + ')';
                Logger.error(this.errorMessage);
                this.isError = true;
                this.isSignOut = await signOut();
                this.errorMessage = 'Sign out: ' + this.isSignOut.toString();
                return; // TODO: should we clear pageInfo here?
              }
              this.pageInfo.clear();
              emitter.emit(EventConstants.onLoginEvent);
            } else {
              this.errorMessage = 'Password failed: ' + result.toString();
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