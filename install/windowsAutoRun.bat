@echo off
echo Hi! I'm Clippy 2.0 and I will help you install or run your WOS or KS Discord Bot.
echo If main.py is missing I will download and install the needed files.
echo :
echo DO NOT TOUCH ANYTHING until you get asked for your bot token!
echo :
echo Not your first time here? Just press enter to skip all my talking ;)
timeout 15

:startUpCheck
IF EXIST main.py (
	goto :venvCheck
	) ELSE (
	goto :firstInstall
)

:firstInstall
	winget install -e --id Python.Python.3.13 --silent --accept-package-agreements --accept-source-agreements
	winget install -e --id Microsoft.VCRedist.2015+.x64 --silent --accept-package-agreements --accept-source-agreements
	winget install -e --id Microsoft.VCRedist.2015+.x86 --silent --accept-package-agreements --accept-source-agreements
	cls
	goto :questionVersion

:installWOS
IF EXIST install.py (
	py install.py
	cls
	echo Woohoo! First install step done, I will make a virtual enviroment now.
	echo Be patient, this takes a minute...
	py -m venv bot_venv
	cls
	echo The enviroment is created, I will continue with the initial startup.
	echo Ignore the scary text in blue you see later, it's all good, I promise!
	echo :
	echo Ready for the next step?
	timeout 10
	goto :startUpCheck
	) ELSE (
	echo :
	echo I will download the latest install.py from GitHub...
	timeout 5
	curl -o install.py https://raw.githubusercontent.com/whiteout-project/install/main/install.py
	goto :installWOS
)

:installKS
IF EXIST install.py (
	py install.py
	cls
	echo Woohoo! First install step done, I will make a virtual enviroment now.
	echo Be patient, this takes a minute...
	py -m venv bot_venv
	cls
	echo The enviroment is created, I will continue with the initial startup.
	echo Ignore the scary text in blue you see later, it's all good, I promise!
	echo :
	echo Ready for the next step?
	timeout 10
	goto :startUpCheck
	) ELSE (
	echo :
	echo I will download the latest install.py from GitHub...
	timeout 5
	curl -o install.py https://raw.githubusercontent.com/kingshot-project/Kingshot-Discord-Bot/main/install/install.py
	goto :installKS
)

:venvCheck
IF EXIST bot_venv\ (
	cls
	echo The bot is going to start now.
	echo Are you excited? I know I am!
	echo :
    bot_venv\Scripts\python.exe main.py --autoupdate
    echo.
    echo Bot stopped. Restarting...
    timeout 3
    goto :venvCheck
	) ELSE (
	py -m venv bot_venv
	echo :
	echo Woohoo, the environment is created. I will start the bot now!
	goto :venvCheck
)

:errorNoPython
	cls
	echo Oh no, it looks like you don't have python installed!
	echo Don't worry, we will get that resolved for you now.
	timeout 5
	winget install -e python3
	winget install -e --id Microsoft.VCRedist.2015+.x86
	winget install -e --id Microsoft.VCRedist.2015+.x64
	goto :firstInstall
	
:questionVersion
	cls
	echo For what game do you want to install the discord bot?
	set /P INPUTA= For Whiteout survival type 1, for kingshot type 2: 
	If /I "%INPUTA%"=="1" goto installWOS
	If /I "%INPUTA%"=="2" goto installKS
	echo Incorrect input & goto questionVersion
