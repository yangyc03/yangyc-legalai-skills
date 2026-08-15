use framework "Foundation"
use scripting additions

on run {input, parameters}
	if (count of input) is not 1 then
		display dialog "请只选择一个目标文件夹。" buttons {"好"} default button "好" with icon caution
		return input
	end if

	set outputPath to missing value
	set temporaryPath to missing value
	try
		set targetFolder to item 1 of input as alias
		set targetPath to POSIX path of targetFolder
		if my pathIsDirectory(targetPath) is false then error "所选项目不是文件夹，请重新选择。" number 1001

		set supportPath to POSIX path of (path to application support from user domain)
		set templatePath to supportPath & "FinderOfficeQuickActions/Word中性空白文档.docx"
		if my pathExists(templatePath) is false then error "未找到 Word 空白模板。请重新运行安装程序，或按安装说明复制模板。" number 1002

		set uuidText to (current application's NSUUID's UUID()'s UUIDString()) as text
		set temporaryPath to targetPath & ".finder-office-word." & uuidText
		my copyItemExact(templatePath, temporaryPath)

		set stamp to do shell script "/bin/date '+%Y-%m-%d_%H%M'"
		set baseName to stamp & "_新建Word文档"
		set itemIndex to 1
		repeat
			if itemIndex is 1 then
				set outputName to baseName & ".docx"
			else
				set outputName to baseName & "_" & my formattedIndex(itemIndex) & ".docx"
			end if
			set outputPath to targetPath & outputName
			if my pathExists(outputPath) is false then
				try
					my moveItemExact(temporaryPath, outputPath)
					exit repeat
				on error moveMessage number moveNumber
					if my pathExists(outputPath) is false then error moveMessage number moveNumber
				end try
			end if
			set itemIndex to itemIndex + 1
		end repeat
	on error errMsg number errNum
		if temporaryPath is not missing value then
			try
				do shell script "/bin/rm -f " & quoted form of temporaryPath
			end try
		end if
		display dialog "无法创建 Word 文档：" & errMsg buttons {"好"} default button "好" with icon stop
		return input
	end try

	try
		do shell script "/usr/bin/open -b com.microsoft.Word " & quoted form of outputPath
	on error errMsg number errNum
		display dialog "Word 文档已经完整创建，但未能用 Microsoft Word 打开：" & errMsg & return & outputPath buttons {"好"} default button "好" with icon caution
	end try
	return input
end run

on pathExists(thePath)
	try
		do shell script "/bin/test -e " & quoted form of thePath
		return true
	on error
		return false
	end try
end pathExists

on pathIsDirectory(thePath)
	try
		do shell script "/bin/test -d " & quoted form of thePath
		return true
	on error
		return false
	end try
end pathIsDirectory

on copyItemExact(sourcePath, targetPath)
	set fileManager to current application's NSFileManager's defaultManager()
	set {didCopy, copyError} to fileManager's copyItemAtPath:sourcePath toPath:targetPath |error|:(reference)
	if (didCopy as boolean) is false then error (copyError's localizedDescription() as text) number 1003
end copyItemExact

on moveItemExact(sourcePath, targetPath)
	set fileManager to current application's NSFileManager's defaultManager()
	set {didMove, moveError} to fileManager's moveItemAtPath:sourcePath toPath:targetPath |error|:(reference)
	if (didMove as boolean) is false then error (moveError's localizedDescription() as text) number 1004
end moveItemExact

on formattedIndex(itemIndex)
	if itemIndex < 10 then return "0" & (itemIndex as text)
	return itemIndex as text
end formattedIndex
