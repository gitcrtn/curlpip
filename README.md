# curlpip
pip fetching HTTPS URLs with curl

## Purpose
The workaround to avoid SSLError with only standard libraries.

## Usage
    curlpip install <package-name1> <pacakge-name2> ...
    curlpip install -r requirements.txt

## Requirement
Python 3.7  
pip  
curl

## Installation
    git clone https://github.com/gitcrtn/curlpip.git
    cd curlpip
    
    chmod +x bin/*
    echo "export PATH=$PWD/bin:$PATH" >> ~/.bashrc

## License
[MIT](https://github.com/gitcrtn/translate/blob/master/LICENSE)

## Author
[Carotene](https://github.com/gitcrtn)
