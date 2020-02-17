package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/ioutil"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"

	"github.com/atotto/clipboard"
)

// ===============

type YouTubePlayerConfig struct {
	StreamingData StreamingData `json:"streamingData"`
	VideoDetails  VideoDetails  `json:"videoDetails"`
	PlayerConfig  PlayerConfig  `json:"playerConfig"`
	Microformat   Microformat   `json:"microformat"`
}

type Microformat struct {
	PlayerMicroformatRenderer PlayerMicroformatRenderer `json:"playerMicroformatRenderer"`
}

type PlayerMicroformatRenderer struct {
	Thumbnail          PlayerMicroformatRendererThumbnail `json:"thumbnail"`
	Title              Description                        `json:"title"`
	Description        Description                        `json:"description"`
	LengthSeconds      string                             `json:"lengthSeconds"`
	OwnerProfileURL    string                             `json:"ownerProfileUrl"`
	IsFamilySafe       bool                               `json:"isFamilySafe"`
	AvailableCountries []string                           `json:"availableCountries"`
	IsUnlisted         bool                               `json:"isUnlisted"`
	HasYpcMetadata     bool                               `json:"hasYpcMetadata"`
	ViewCount          string                             `json:"viewCount"`
	Category           string                             `json:"category"`
	PublishDate        string                             `json:"publishDate"`
	OwnerChannelName   string                             `json:"ownerChannelName"`
	UploadDate         string                             `json:"uploadDate"`
}

type Description struct {
	SimpleText string `json:"simpleText"`
}

type PlayerMicroformatRendererThumbnail struct {
	Thumbnails []ThumbnailElement `json:"thumbnails"`
}

type ThumbnailElement struct {
	URL    string `json:"url"`
	Width  int64  `json:"width"`
	Height int64  `json:"height"`
}

type PlayerConfig struct {
	AudioConfig           AudioConfig           `json:"audioConfig"`
	StreamSelectionConfig StreamSelectionConfig `json:"streamSelectionConfig"`
	MediaCommonConfig     MediaCommonConfig     `json:"mediaCommonConfig"`
}

type AudioConfig struct {
	LoudnessDB              float64 `json:"loudnessDb"`
	PerceptualLoudnessDB    float64 `json:"perceptualLoudnessDb"`
	EnablePerFormatLoudness bool    `json:"enablePerFormatLoudness"`
}

type MediaCommonConfig struct {
	DynamicReadaheadConfig DynamicReadaheadConfig `json:"dynamicReadaheadConfig"`
}

type DynamicReadaheadConfig struct {
	MaxReadAheadMediaTimeMS int64 `json:"maxReadAheadMediaTimeMs"`
	MinReadAheadMediaTimeMS int64 `json:"minReadAheadMediaTimeMs"`
	ReadAheadGrowthRateMS   int64 `json:"readAheadGrowthRateMs"`
}

type StreamSelectionConfig struct {
	MaxBitrate string `json:"maxBitrate"`
}

type StreamingData struct {
	ExpiresInSeconds string   `json:"expiresInSeconds"`
	Formats          []Format `json:"formats"`
	AdaptiveFormats  []Format `json:"adaptiveFormats"`
}

type Format struct {
	Itag             int64  `json:"itag"`
	MIMEType         string `json:"mimeType"`
	Bitrate          int64  `json:"bitrate"`
	Width            int64  `json:"width,omitempty"`
	Height           int64  `json:"height,omitempty"`
	InitRange        Range  `json:"initRange,omitempty"`
	IndexRange       Range  `json:"indexRange,omitempty"`
	LastModified     string `json:"lastModified"`
	ContentLength    string `json:"contentLength"`
	Quality          string `json:"quality"`
	FPS              int64  `json:"fps,omitempty"`
	QualityLabel     string `json:"qualityLabel,omitempty"`
	ProjectionType   string `json:"projectionType"`
	AverageBitrate   int64  `json:"averageBitrate"`
	ApproxDurationMS string `json:"approxDurationMs"`
	Cipher           string `json:"cipher"`
	HighReplication  bool   `json:"highReplication,omitempty"`
	AudioQuality     string `json:"audioQuality,omitempty"`
	AudioSampleRate  string `json:"audioSampleRate,omitempty"`
	AudioChannels    int64  `json:"audioChannels,omitempty"`
}

type Range struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

type VideoDetails struct {
	VideoID           string                             `json:"videoId"`
	Title             string                             `json:"title"`
	LengthSeconds     string                             `json:"lengthSeconds"`
	Keywords          []string                           `json:"keywords"`
	ChannelID         string                             `json:"channelId"`
	IsOwnerViewing    bool                               `json:"isOwnerViewing"`
	ShortDescription  string                             `json:"shortDescription"`
	IsCrawlable       bool                               `json:"isCrawlable"`
	Thumbnail         PlayerMicroformatRendererThumbnail `json:"thumbnail"`
	AverageRating     float64                            `json:"averageRating"`
	AllowRatings      bool                               `json:"allowRatings"`
	ViewCount         string                             `json:"viewCount"`
	Author            string                             `json:"author"`
	IsPrivate         bool                               `json:"isPrivate"`
	IsUnpluggedCorpus bool                               `json:"isUnpluggedCorpus"`
	IsLiveContent     bool                               `json:"isLiveContent"`
}

// ===============

// TODO: figure out how to determine if video is unavailable or not

var (
	// YTPlayerConfigRegex              = regexp.MustCompile(`;yt\.setConfig\(\{'PLAYER_CONFIG':\s*({.*})(,'EXPERIMENT_FLAGS'|;)`)
	VideoIDRegex                     = regexp.MustCompile(`(?:v=|\/)([0-9A-Za-z_-]{11}).*`)
	AgeRestrictedRegex               = regexp.MustCompile(`og:restrictions:age`)
	AgeRestrictedSTSRegex            = regexp.MustCompile(`"sts"\s*:\s*(\d+)`)
	YTPlayerConfigRegex              = regexp.MustCompile(`;\s*ytplayer\.config\s*=\s*({.*?});`)
	YTPlayerConfigEmbedRegex         = regexp.MustCompile(`yt\.setConfig\(\s*\{\s*'PLAYER_CONFIG':\s*(\{.*\})\s*\}\s*\)\s*;\s*writeEmbed`)
	YTPlayerConfigAgeRestrictedRegex = regexp.MustCompile(`;ytplayer\.config\s*=\s*({.*?});`)
)

type YouTubeVideoInfo struct {
	Assets YouTubeVideoInfoAssets `json:"assets"`
	Args   YouTubeVideoInfoArgs   `json:"args"`
	// Args map[string]interface{} `json:"args"`
}

type YouTubeVideoInfoAssets struct {
	JS string `json:"js"`
}

type YouTubeStreamInfo struct {
	URL     string
	S       string
	Type    string
	Quality string
	ITag    int64
}

type YouTubeVideoInfoArgs struct {
	RegularPlayerResponse  string `json:"player_response"`
	EmbeddedPlayerResponse string `json:"embedded_player_response"`
}

type YouTube struct {
	URL     string
	Streams []YouTubeStreamInfo
}

func youtubeGetRequest(URL string) (*http.Request, error) {
	youtubeRequest, err := http.NewRequest(http.MethodGet, URL, nil)

	if err != nil {
		return nil, err
	}

	youtubeRequest.Header.Set("User-Agent", "Mozilla/5.0")

	return youtubeRequest, nil
}

func newYouTube(URL string) (*YouTube, error) {
	var youtube YouTube

	videoIDMatch := VideoIDRegex.FindStringSubmatch(URL)

	if len(videoIDMatch) != 2 {
		return nil, errors.New("could not parse video ID from url")
	}

	videoID := videoIDMatch[1]

	watchURL := "https://youtube.com/watch?v=" + videoID
	// embedURL := "https://www.youtube.com/embed/" + videoID

	getWatchHTMLRequest, err := http.NewRequest(http.MethodGet, watchURL, nil)

	if err != nil {
		return nil, err
	}

	getWatchHTMLRequest.Header.Set("User-Agent", "Mozilla/5.0")

	watchHTMLResponse, err := http.DefaultClient.Do(getWatchHTMLRequest)

	if err != nil {
		return nil, err
	}

	watchHTMLBytes, err := ioutil.ReadAll(watchHTMLResponse.Body)

	watchHTMLResponse.Body.Close()

	if err != nil {
		return nil, err
	}

	if len(watchHTMLBytes) == 0 {
		// Former naive way of determining if video is unavailable, would fail
		// if video was age restricted
		//|| !bytes.Contains(watchHTMLBytes, []byte(`<img class="icon meh" src="/yts/img`)) {
		return nil, errors.New("video is unavailable")
	}

	isAgeRestricted := AgeRestrictedRegex.Match(watchHTMLBytes)

	videoInfoURL, err := url.Parse("https://youtube.com/get_video_info")

	if err != nil {
		return nil, err
	}

	videoInfoParams := url.Values{}

	videoInfoParams.Add("video_id", videoID)

	if isAgeRestricted {
		videoInfoParams.Add("eurl", fmt.Sprintf("https://youtube.googleapis.com/v/%s", videoID))

		// stsMatch := AgeRestrictedSTSRegex.FindSubmatch(embedHTMLBytes)

		// fmt.Println(embedHTMLResponse.StatusCode, len(embedHTMLBytes))

		// clipboard.WriteAll(string(embedHTMLBytes))

		// if len(stsMatch) != 2 {
		// 	return nil, errors.New("could not parse \"sts\" for age-restricted video")
		// }

		// videoInfoParams.Add("sts", string(stsMatch[1]))
	}

	videoInfoURL.RawQuery = videoInfoParams.Encode()

	getVideoInfoRequest, err := youtubeGetRequest(videoInfoURL.String())

	if err != nil {
		return nil, err
	}

	getVideoInfoResponse, err := http.DefaultClient.Do(getVideoInfoRequest)

	if err != nil {
		return nil, err
	}

	videoInfoRawBytes, err := ioutil.ReadAll(getVideoInfoResponse.Body)

	getVideoInfoResponse.Body.Close()

	if err != nil {
		return nil, err
	}

	// var videoInfoMatch [][]byte

	// if isAgeRestricted {
	// 	videoInfoMatch = YTPlayerConfigEmbedRegex.FindSubmatch(embedHTMLBytes)

	// 	if len(videoInfoMatch) == 0 {
	// 		return nil, errors.New("couldn't find video info bytes for age restricted video")
	// 	}
	// } else {

	// }

	var youtubeVideoInfo YouTubeVideoInfo
	var youtubePlayerConfig YouTubePlayerConfig

	if isAgeRestricted {
		videoInfoValues, err := url.ParseQuery(string(videoInfoRawBytes))

		if err != nil {
			return nil, err
		}

		err = json.Unmarshal([]byte(videoInfoValues.Get("player_response")), &youtubePlayerConfig)

		if err != nil {
			return nil, err
		}

		getEmbedHTMLRequest, err := youtubeGetRequest("https://www.youtube.com/embed/" + videoID)

		if err != nil {
			return nil, err
		}

		embedHTMLResponse, err := http.DefaultClient.Do(getEmbedHTMLRequest)

		if err != nil {
			return nil, err
		}

		embedHTMLBytes, err := ioutil.ReadAll(embedHTMLResponse.Body)

		embedHTMLResponse.Body.Close()

		if err != nil {
			return nil, err
		}

		embedYTPlayerConfigMatch := YTPlayerConfigEmbedRegex.FindSubmatch(embedHTMLBytes)

		if len(embedYTPlayerConfigMatch) != 2 {
			return nil, err
		}

		err = json.Unmarshal(embedYTPlayerConfigMatch[1], &youtubeVideoInfo)

		clipboard.WriteAll(videoInfoValues.Get("player_response"))
	} else {
		videoInfoMatch := YTPlayerConfigRegex.FindSubmatch(watchHTMLBytes)

		if len(videoInfoMatch) != 2 {
			return nil, errors.New("couldn't find video info bytes for video")
		}

		err = json.Unmarshal(videoInfoMatch[1], &youtubeVideoInfo)

		if err != nil {
			return nil, err
		}

		err = json.Unmarshal([]byte(youtubeVideoInfo.Args.RegularPlayerResponse), &youtubePlayerConfig)
	}

	if err != nil {
		return nil, err
	}

	fmt.Println(isAgeRestricted, youtubePlayerConfig.VideoDetails.Author)

	jsURL := fmt.Sprintf("https://youtube.com%s", youtubeVideoInfo.Assets.JS)
	var rawJS []byte

	youtube.Streams = make([]YouTubeStreamInfo, 0)

	for _, stream := range youtubePlayerConfig.StreamingData.AdaptiveFormats {
		streamValues, err := url.ParseQuery(stream.Cipher)

		if err != nil {
			return nil, err
		}

		streamInfo := YouTubeStreamInfo{
			ITag:    stream.Itag,
			Quality: stream.Quality,
			S:       streamValues.Get("s"),
			Type:    stream.MIMEType,
			URL:     streamValues.Get("url"),
		}

		if !(strings.Contains(streamInfo.URL, "signature") || (!strings.Contains(streamInfo.URL, "s") && (strings.Contains(streamInfo.URL, "sig=") || strings.Contains(streamInfo.URL, "lsig=")))) {
			if len(rawJS) == 0 {
				getJSRequest, err := youtubeGetRequest(jsURL)

				if err != nil {
					return nil, err
				}

				getJSResponse, err := http.DefaultClient.Do(getJSRequest)

				if err != nil {
					return nil, err
				}

				rawJS, err = ioutil.ReadAll(getJSResponse.Body)

				getJSResponse.Body.Close()

				if err != nil {
					return nil, err
				}
			}
			// Decipher here

			// Transform Plan Func

			transformFunctionRegexStrings := []string{
				`\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\b(?P<sig>[a-zA-Z0-9$]{2})\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)`,
				`(?P<sig>[a-zA-Z0-9$]+)\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)`,
				`(["\'])signature\1\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\.sig\|\|(?P<sig>[a-zA-Z0-9$]+)\(`,
				`yt\.akamaized\.net/\)\s*\|\|\s*.*?\s*[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?:encodeURIComponent\s*\()?\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\bc\s*&&\s*a\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
				`\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(`,
			}

			initialFunctionName := ""

			for _, transformFunctionRegexString := range transformFunctionRegexStrings {
				transformFunctionRegex := regexp.MustCompile(transformFunctionRegexString)

				transformFuncMatch := transformFunctionRegex.FindSubmatch(rawJS)

				if len(transformFuncMatch) == 2 {
					initialFunctionName = string(transformFuncMatch[1])

					break
				}
			}

			if initialFunctionName == "" {
				return nil, errors.New("could not find transform initial function name")
			}

			transformPlanRegex := regexp.MustCompile(fmt.Sprintf(`%s=function\(\w\){[a-z=\.\(\"\)]*;(.*);(?:.+)}`, regexp.QuoteMeta(initialFunctionName)))

			transformPlanMatch := transformPlanRegex.FindSubmatch(rawJS)

			if len(transformPlanMatch) != 2 {
				return nil, errors.New("could not find transform plan function name")
			}

			transformPlan := strings.Split(string(transformPlanMatch[1]), ";")

			// Get Transform Map Func

			initialVariable := strings.Split(transformPlan[0], ".")[0]

			// Check here if trouble, otherwise do length check so no panic
			transformObjectRegex := regexp.MustCompile(fmt.Sprintf(`(?s)var %s={(.*?)};`, regexp.QuoteMeta(initialVariable)))

			transformObjectRegexResults := transformObjectRegex.FindSubmatch(rawJS)

			if len(transformObjectRegexResults) != 2 {
				return nil, errors.New("could not find transform object")
			}

			transformObjects := strings.Split(strings.Replace(string(transformObjectRegexResults[1]), "\n", " ", -1), ", ")

			functionsMap := make(map[string]func(string, int) string)

			functionRegexStringsMap := map[string]func(string, int) string{
				// function(a){a.reverse()}
				`{\w\.reverse\(\)}`: reverse, // Reverse
				// function(a,b){a.splice(0,b)}
				`{\w\.splice\(0,\w\)}`: splice, // Splice
				// function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}
				`{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\]=\w}`: swap, // Swap
				// function(a,b){var c=a[0];a[0]=a[b%a.length];a[b%a.length]=c}
				`{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\%\w.length\]=\w}`: swap, // Swap
			}

			for _, tranformsObject := range transformObjects {
				name, function := strings.Split(tranformsObject, ":")[0], strings.Split(tranformsObject, ":")[1]

				for functionRegexString, equivalentFunction := range functionRegexStringsMap {
					if regexp.MustCompile(functionRegexString).MatchString(function) {
						functionsMap[name] = equivalentFunction

						break
					}
				}
			}

			signature := streamInfo.S

			for _, jsFunction := range transformPlan {
				// Parse func
				jsFunctionMatch := regexp.MustCompile(`\w+\.(\w+)\(\w,(\d+)\)`).FindStringSubmatch(jsFunction)

				if len(jsFunctionMatch) != 3 {
					return nil, errors.New("could not parse JS function in transform plan")
				}

				functionName, functionArgString := jsFunctionMatch[1], jsFunctionMatch[2]

				functionArg, err := strconv.Atoi(functionArgString)

				if err != nil {
					return nil, err
				}

				signature = functionsMap[functionName](signature, functionArg)
			}

			if streamInfo.URL[len(streamInfo.URL)-1] == '&' {
				streamInfo.URL = streamInfo.URL[:len(streamInfo.URL)-2]
			}

			streamInfo.URL = fmt.Sprintf("%s&sig=%s", streamInfo.URL, signature)
		}

		youtube.Streams = append(youtube.Streams, streamInfo)
	}

	return &youtube, nil
}

func reverse(signature string, _ int) string {
	n := 0
	rune := make([]rune, len(signature))

	for _, r := range signature {
		rune[n] = r
		n++
	}

	rune = rune[0:n]

	// Reverse
	for i := 0; i < n/2; i++ {
		rune[i], rune[n-1-i] = rune[n-1-i], rune[i]
	}

	// Convert back to UTF-8.
	return string(rune)
}

func splice(signature string, index int) string {
	return signature[:index] + signature[index*2:]
}

func swap(signature string, index int) string {
	r := index % len(signature)

	return string(signature[r]) + signature[1:r] + string(signature[0]) + signature[r+1:]
}

var audioExtensionMap = map[string]string{
	"mp4": "m4a",
}

func main() {
	youtube, err := newYouTube("https://www.youtube.com/watch?v=DBzuYNK95sM")

	if err != nil {
		panic(err)
	}

	for _, stream := range youtube.Streams {
		if strings.HasPrefix(stream.Type, "audio/mp4") {
			videoDownloadRequest, err := youtubeGetRequest(stream.URL)

			if err != nil {
				panic(err)
			}

			videoDownloadResponse, err := http.DefaultClient.Do(videoDownloadRequest)

			if err != nil {
				panic(err)
			}

			downloadFile, err := os.Create("nice.mp4")

			io.Copy(downloadFile, videoDownloadResponse.Body)

			videoDownloadResponse.Body.Close()

			downloadFile.Close()
		}
	}

	// _, err = newYouTube("https://www.youtube.com/watch?v=Fl-e2PM1ITM&bpctr=1581197742")

	// if err != nil {
	// 	panic(err)
	// }
}
